# engine/funding_arb_engine.py
import asyncio, logging, time
from config import (
    API_KEYS, PAPER_TRADING_MODE, FUNDING_RATE_ALERT_APR_PCT,
    FUNDING_ARB_ENABLED, FUNDING_ARB_SYMBOL_SPOT, FUNDING_ARB_SYMBOL_PERP,
    FUNDING_ARB_TRADE_SIZE_USD, FUNDING_ARB_LEVERAGE, FUNDING_ARB_ENTRY_APR_PCT,
    FUNDING_ARB_EXIT_APR_PCT, FUNDING_ARB_MAX_ENTRY_BASIS_PCT,
    FUNDING_ARB_MAX_HOLD_BASIS_PCT, FUNDING_ARB_MARGIN_SAFETY_PCT,
    FUNDING_ARB_POLL_INTERVAL_SEC, MAX_TRADE_SIZE_USD
)

class FundingArbEngine:
    """
    Arbitrage de taux de financement (cash-and-carry delta-neutre) :
    achète le spot ET vend à découvert le même montant en perpétuel en même
    temps. Le prix devient neutre (si BTC monte ou descend, les deux jambes
    se compensent), et la stratégie encaisse le funding payé par les
    positions à effet de levier long toutes les 8h.

    CE N'EST PAS "GARANTI SANS RISQUE", malgré le nom "arbitrage" :
    - Risque de liquidation sur la jambe perpétuelle : l'exchange évalue
      cette jambe SEULE pour la marge, pas la position nette couverte. Si
      le prix monte fortement et que la marge n'est pas suffisante, la
      jambe short peut être liquidée même si la jambe spot compense en
      théorie. D'où un levier volontairement bas (FUNDING_ARB_LEVERAGE) et
      une surveillance active de la distance au prix de liquidation.
    - Risque de base : spot et perpétuel ne sont pas exactement le même
      prix à tout instant (basis). Un écart qui se creuse pendant qu'on est
      en position dégrade la couverture réelle.
    - Le funding peut redevenir défavorable (ou négatif) : on sort si l'APR
      tombe sous un second seuil, plus bas que celui d'entrée.
    - Risque d'exécution classique (déjà géré comme partout ailleurs dans
      ce bot) : jamais suppose qu'un ordre "accepté" est rempli, jambe
      perp vérifiée par sa position réelle après coup, dénouement de la
      jambe spot si la jambe perp échoue.

    Si les clés BinanceFutures ne sont pas configurées, se comporte comme
    l'ancien FundingRateEngine (monitoring/alerte seulement, aucune
    exécution) -- ce fichier remplace ce moteur pour éviter deux connexions
    ccxt.binanceusdm distinctes qui interrogeraient la même API en double.
    Si FUNDING_ARB_ENABLED=False, même chose : monitoring seulement, sans
    toucher au code.

    Taille de position volontairement FIXE (pas de réinvestissement
    composé comme sur le triangulaire/cross-exchange) : ici le capital
    engagé sert aussi de marge sur une position à effet de levier, donc
    augmenter la taille augmente aussi le risque de liquidation, pas
    seulement l'exposition -- une décision à prendre explicitement, pas à
    automatiser en silence.
    """
    def __init__(self, order_manager, futures_manager, notifier, trade_logger=None):
        self._order_manager = order_manager
        self.futures_manager = futures_manager
        self.notifier = notifier
        self.trade_logger = trade_logger
        self.logger = logging.getLogger(self.__class__.__name__)

        self.spot_symbol = FUNDING_ARB_SYMBOL_SPOT
        self.perp_symbol = FUNDING_ARB_SYMBOL_PERP
        self.base_asset = self.spot_symbol.split('/')[0]
        self.enabled = FUNDING_ARB_ENABLED
        self.trade_size_usd = FUNDING_ARB_TRADE_SIZE_USD
        self.leverage = FUNDING_ARB_LEVERAGE
        self.entry_apr_pct = FUNDING_ARB_ENTRY_APR_PCT
        self.exit_apr_pct = FUNDING_ARB_EXIT_APR_PCT
        self.max_entry_basis_pct = FUNDING_ARB_MAX_ENTRY_BASIS_PCT
        self.max_hold_basis_pct = FUNDING_ARB_MAX_HOLD_BASIS_PCT
        self.margin_safety_pct = FUNDING_ARB_MARGIN_SAFETY_PCT
        self.poll_interval = FUNDING_ARB_POLL_INTERVAL_SEC
        self.alert_apr_pct = FUNDING_RATE_ALERT_APR_PCT

        self.in_position = False
        self.entry_time_ms = None
        self.spot_amount = 0.0
        self.perp_amount = 0.0
        self.spot_entry_cost_usd = 0.0
        self.perp_entry_avg = 0.0
        self.entry_apr_at_open = None

        # État d'une sortie partiellement réussie (une jambe fermée, l'autre
        # pas) -- conservé entre deux tentatives pour ne jamais retenter de
        # fermer une jambe déjà fermée (vendre un spot déjà vendu, etc.) et
        # pour pouvoir calculer le P&L complet une fois les deux fermées.
        self._exit_perp_avg = None
        self._exit_perp_amount_closed = None
        self._exit_spot_proceeds = None
        self._exit_spot_amount_closed = None

        self.session_pnl_usd = 0.0
        self.max_session_loss_usd = MAX_TRADE_SIZE_USD * 10
        self._halted = False
        self._last_alerted_apr = None

    def _configured(self) -> bool:
        return self.futures_manager.is_configured()

    async def run(self):
        if not self._configured():
            self.logger.warning(
                "BinanceFutures API keys not configured (BINANCE_FUTURES_API_KEY/SECRET). "
                "Funding Arb Engine is inactive. Generate Demo Trading keys separately at "
                "https://demo.binance.com/en/my/settings/api-management if you want this engine "
                "to run -- your spot testnet keys will not work here, and neither will old "
                "testnet.binancefuture.com keys (Binance retired sandbox mode for futures)."
            )
            return

        try:
            await self.futures_manager.initialize()
        except Exception as e:
            self.logger.error(f"Funding Arb Engine failed to initialize futures connection: {e}", exc_info=True)
            return

        await self._recover_existing_position()

        mode = "EXECUTION" if self.enabled else "MONITORING ONLY (FUNDING_ARB_ENABLED=False)"
        self.logger.info(f"Funding Arb Engine running -- spot={self.spot_symbol} / perp={self.perp_symbol} | mode={mode}")

        try:
            while True:
                await self._tick()
                await asyncio.sleep(self.poll_interval)
        finally:
            await self.futures_manager.close()

    async def _recover_existing_position(self):
        # Si le process a redémarré alors qu'une position était déjà ouverte
        # (crash, redéploiement...), on la retrouve plutôt que de repartir à
        # zéro -- sinon on risquerait d'en ouvrir une seconde par-dessus, ou
        # d'ignorer une position réelle dont personne ne surveille plus le
        # risque de marge. Le coût d'entrée reconstruit est approximatif.
        position = await self.futures_manager.get_position(self.perp_symbol)
        if not position or not position.get('contracts'):
            return
        self.in_position = True
        self.perp_amount = position.get('contracts')
        self.perp_entry_avg = position.get('entryPrice') or 0.0
        spot_balance = await self._order_manager.get_balance('Binance', self.base_asset)
        self.spot_amount = spot_balance if spot_balance else self.perp_amount
        self.spot_entry_cost_usd = self.spot_amount * self.perp_entry_avg
        self.entry_time_ms = int(time.time() * 1000)
        self.logger.warning(
            f"[FundingArb] Resumed after restart: found an existing perp position "
            f"({self.perp_amount} {self.perp_symbol}). Resuming monitoring -- entry cost is "
            f"an approximation, and funding tracking restarts from now (pre-restart funding is not counted)."
        )
        await self.notifier.send_message(
            f"⚠️ *Funding Arb Resumed* ⚠️\nFound an existing open position after restart "
            f"({self.perp_amount:.6f} {self.perp_symbol}).\nResuming monitoring; entry P&L baseline is approximate."
        )

    async def _tick(self):
        if self._halted:
            return
        try:
            info = await self.futures_manager.exchange.fetch_funding_rate(self.perp_symbol)
        except Exception as e:
            self.logger.error(f"[FundingArb] Failed to fetch funding rate for {self.perp_symbol}: {e}")
            return

        rate = info.get('fundingRate')
        if rate is None:
            self.logger.warning(f"[FundingArb] No fundingRate field in response for {self.perp_symbol}.")
            return

        apr_pct = rate * 3 * 365 * 100
        pos_str = f"long {self.spot_amount:.6f} {self.base_asset} / short {self.perp_amount:.6f} perp" if self.in_position else "flat"
        self.logger.info(f"[FundingArb] {self.perp_symbol} funding={rate*100:.4f}%/8h -> ~{apr_pct:+.2f}% APR | position={pos_str} | session P&L=${self.session_pnl_usd:+.4f}")

        if self.in_position:
            await self._monitor_position(apr_pct)
            return

        if not self.enabled:
            if abs(apr_pct) >= self.alert_apr_pct and self._last_alerted_apr != round(apr_pct, 1):
                direction = "longs payent les shorts (spot long + perp short serait rémunéré)" if apr_pct > 0 else "shorts payent les longs (non exploitable sans short sur le spot)"
                await self.notifier.send_message(f"💰 *Funding Rate Alert* 💰\n{self.perp_symbol}: ~{apr_pct:+.2f}% APR\n{direction}\n(Monitoring seulement -- FUNDING_ARB_ENABLED=False)")
                self._last_alerted_apr = round(apr_pct, 1)
                if self.trade_logger:
                    self.trade_logger.log_trade(event_type='FUNDING_RATE_ALERT', symbol=self.perp_symbol, details=f"apr_pct={apr_pct:.2f}, raw_rate={rate}")
            return

        if apr_pct >= self.entry_apr_pct:
            await self._try_enter(apr_pct)

    async def _get_spot_price(self):
        try:
            ticker = await self._order_manager.exchanges['Binance'].fetch_ticker(self.spot_symbol)
            return ticker.get('ask') or ticker.get('last')
        except Exception as e:
            self.logger.error(f"[FundingArb] Failed to fetch spot ticker for {self.spot_symbol}: {e}")
            return None

    async def _try_enter(self, apr_pct):
        spot_price = await self._get_spot_price()
        perp_price = await self.futures_manager.get_mark_price(self.perp_symbol)
        if not spot_price or not perp_price:
            self.logger.warning("[FundingArb] Could not fetch prices for entry basis check, skipping this cycle.")
            return

        basis_pct = (perp_price - spot_price) / spot_price * 100
        if abs(basis_pct) > self.max_entry_basis_pct:
            self.logger.info(f"[FundingArb] Basis too wide to enter safely ({basis_pct:+.4f}% > {self.max_entry_basis_pct}%), skipping despite attractive funding ({apr_pct:+.2f}% APR).")
            return

        self.logger.warning(f"[FundingArb] Entry signal: funding ~{apr_pct:+.2f}% APR, basis {basis_pct:+.4f}%. Opening hedge.")

        leverage_ok = await self.futures_manager.set_leverage(self.perp_symbol, self.leverage)
        if not leverage_ok:
            self.logger.error("[FundingArb] Could not confirm leverage setting -- aborting entry rather than opening a position under an unknown margin assumption.")
            return

        spot_order = await self._order_manager.create_market_order('Binance', self.spot_symbol, 'buy', cost=self.trade_size_usd)
        if not spot_order or not spot_order.get('filled'):
            self.logger.error("[FundingArb] Spot leg failed to fill. No position taken.")
            return

        base_amount = spot_order['filled']
        perp_order = await self.futures_manager.create_market_order(self.perp_symbol, 'sell', base_amount)

        # Ne pas se fier au seul "ordre accepté" : on vérifie que la position
        # réelle sur l'exchange correspond à ce qu'on attend (short, même
        # taille) avant de considérer la couverture comme réellement ouverte.
        position = await self.futures_manager.get_position(self.perp_symbol) if perp_order and perp_order.get('filled') else None
        perp_ok = bool(
            perp_order and perp_order.get('filled') and position
            and position.get('side') == 'short'
            and base_amount > 0
            and abs((position.get('contracts') or 0) - base_amount) / base_amount < 0.02
        )

        if not perp_ok:
            self.logger.error("[FundingArb] Perp short leg failed or size mismatch -- unwinding spot leg immediately.")
            await self._unwind_spot_only(base_amount, spot_order)
            return

        self.in_position = True
        self.entry_time_ms = int(time.time() * 1000)
        self.spot_amount = base_amount
        self.perp_amount = position.get('contracts') or base_amount
        self.spot_entry_cost_usd = spot_order.get('cost') or (base_amount * (spot_order.get('average') or 0))
        self.perp_entry_avg = perp_order.get('average') or 0.0
        self.entry_apr_at_open = apr_pct

        self.logger.info(f"[FundingArb] Hedge opened: long {base_amount:.6f} {self.base_asset} spot @ ~${self.spot_entry_cost_usd:.4f}, short {self.perp_amount:.6f} perp @ ~{self.perp_entry_avg}. Funding at entry: {apr_pct:+.2f}% APR.")
        await self.notifier.send_message(f"🟢 *Funding Arb Entry* 🟢\nLong {base_amount:.6f} {self.base_asset} spot / Short {self.perp_amount:.6f} perp\nFunding: {apr_pct:+.2f}% APR")
        if self.trade_logger:
            self.trade_logger.log_trade(
                event_type='FUNDING_ARB_ENTRY', platform_buy='Binance', symbol=self.spot_symbol,
                volume=base_amount, buy_price=self.spot_entry_cost_usd / base_amount if base_amount else 0,
                details=f"perp_symbol={self.perp_symbol}, apr_pct={apr_pct:.2f}, spot_order_id={spot_order['id']}, perp_order_id={perp_order['id']}"
            )

    async def _unwind_spot_only(self, base_amount, spot_order):
        unwind = await self._order_manager.create_market_order('Binance', self.spot_symbol, 'sell', amount=base_amount)
        if unwind and unwind.get('filled'):
            recovered = unwind.get('cost') or (unwind['filled'] * (unwind.get('average') or 0))
            loss = recovered - (spot_order.get('cost') or self.trade_size_usd)
            self.logger.info(f"[FundingArb] Unwound spot leg. P&L: ${loss:+.4f}")
            await self._apply_breaker(loss)
            if self.trade_logger:
                self.trade_logger.log_trade(event_type='FUNDING_ARB_UNWOUND', platform_buy='Binance', platform_sell='Binance', symbol=self.spot_symbol, volume=base_amount, profit_usd=loss, details="perp leg failed or mismatched, unwound spot")
        else:
            self.logger.critical("[FundingArb] UNHEDGED POSITION: spot leg open, perp leg failed, and unwind ALSO failed. Manual intervention required.")
            await self.notifier.send_message(f"🔥 *UNHEDGED POSITION (Funding Arb)* 🔥\nBought {base_amount:.6f} {self.base_asset} spot but the perp short failed AND unwinding the spot leg also failed. Manual intervention required.")
            if self.trade_logger:
                self.trade_logger.log_trade(event_type='FUNDING_ARB_UNHEDGED', platform_buy='Binance', symbol=self.spot_symbol, volume=base_amount, details="perp leg failed, unwind also failed")

    async def _monitor_position(self, apr_pct):
        position = await self.futures_manager.get_position(self.perp_symbol)
        spot_price = await self._get_spot_price()
        perp_price = (position.get('markPrice') if position else None) or await self.futures_manager.get_mark_price(self.perp_symbol)

        # Priorité absolue : le risque de marge passe avant tout le reste,
        # même si le funding reste par ailleurs attractif.
        if position:
            liq_price = position.get('liquidationPrice')
            mark_price = position.get('markPrice') or perp_price
            if liq_price and mark_price:
                distance_pct = abs(liq_price - mark_price) / mark_price * 100
                if distance_pct <= self.margin_safety_pct:
                    self.logger.critical(f"[FundingArb] Margin safety breached: only {distance_pct:.2f}% from liquidation price. Closing immediately.")
                    await self._exit_position(reason="margin-risk")
                    return

        if spot_price and perp_price:
            basis_pct = (perp_price - spot_price) / spot_price * 100
            if abs(basis_pct) > self.max_hold_basis_pct:
                self.logger.warning(f"[FundingArb] Basis diverged too far while holding ({basis_pct:+.4f}% > {self.max_hold_basis_pct}%), exiting hedge.")
                await self._exit_position(reason="basis-divergence")
                return

        if apr_pct < self.exit_apr_pct:
            self.logger.info(f"[FundingArb] Funding dropped below exit threshold ({apr_pct:+.2f}% < {self.exit_apr_pct}%). Closing.")
            await self._exit_position(reason="funding-below-threshold")

    async def _exit_position(self, reason):
        # Chaque jambe déjà fermée (self.perp_amount / self.spot_amount mis à
        # 0 dès qu'un close réussit) est sautée lors d'une nouvelle tentative
        # -- sinon un retry après un échec partiel revendrait un spot déjà
        # vendu, ou re-tenterait de fermer un perp déjà clôturé.
        perp_amount_to_close = 0.0
        if self.perp_amount > 0:
            # Toujours re-vérifier la taille réelle de la position perp juste
            # avant de la fermer (reduceOnly), plutôt que de se fier au
            # chiffre suivi localement qui peut avoir dérivé.
            position = await self.futures_manager.get_position(self.perp_symbol)
            perp_amount_to_close = (position.get('contracts') if position else self.perp_amount) or 0.0

        spot_amount_to_close = self.spot_amount

        if perp_amount_to_close <= 0 and spot_amount_to_close <= 0:
            # Les deux jambes sont déjà fermées (retry après un échec partiel
            # dont l'autre jambe vient d'être terminée) : finalise directement.
            await self._finalize_exit(reason)
            return

        self.logger.warning(
            f"[FundingArb] Exiting hedge ({reason}). Closing {spot_amount_to_close:.6f} spot / "
            f"{perp_amount_to_close:.6f} perp (already-closed legs are skipped)."
        )

        perp_close = None
        if perp_amount_to_close > 0:
            perp_close = await self.futures_manager.create_market_order(self.perp_symbol, 'buy', perp_amount_to_close, reduce_only=True)
            if perp_close and perp_close.get('filled'):
                self._exit_perp_avg = perp_close.get('average') or 0.0
                self._exit_perp_amount_closed = perp_amount_to_close
                self.perp_amount = 0.0
            else:
                perp_close = None

        spot_close = None
        if spot_amount_to_close > 0:
            spot_close = await self._order_manager.create_market_order('Binance', self.spot_symbol, 'sell', amount=spot_amount_to_close)
            if spot_close and spot_close.get('filled'):
                self._exit_spot_proceeds = spot_close.get('cost') or (spot_close['filled'] * (spot_close.get('average') or 0))
                self._exit_spot_amount_closed = spot_amount_to_close
                self.spot_amount = 0.0
            else:
                spot_close = None

        perp_ok = perp_amount_to_close <= 0 or perp_close is not None
        spot_ok = spot_amount_to_close <= 0 or spot_close is not None

        if perp_ok and spot_ok:
            await self._finalize_exit(reason)
        else:
            self.logger.critical(f"[FundingArb] UNHEDGED POSITION while exiting ({reason}): perp_ok={perp_ok}, spot_ok={spot_ok}. Will retry the remaining leg(s) on the next tick.")
            await self.notifier.send_message(f"🔥 *UNHEDGED POSITION (Funding Arb exit)* 🔥\nFailed to fully close the hedge ({reason}).\nperp closed: {perp_ok}, spot closed: {spot_ok}\nWill retry the remaining leg(s). Manual intervention advised if this persists.")
            if self.trade_logger:
                self.trade_logger.log_trade(event_type='FUNDING_ARB_UNHEDGED', symbol=self.spot_symbol, volume=spot_amount_to_close, details=f"exit failed: reason={reason}, perp_ok={perp_ok}, spot_ok={spot_ok}")
            # On laisse in_position=True (et les jambes déjà fermées à 0) :
            # mieux vaut re-tenter la jambe restante au prochain tick que de
            # "perdre" une position à moitié fermée en remettant l'état à zéro.

    async def _finalize_exit(self, reason):
        funding_collected = await self.futures_manager.fetch_funding_since(self.perp_symbol, self.entry_time_ms)

        spot_pnl = (self._exit_spot_proceeds or 0.0) - self.spot_entry_cost_usd
        perp_price_pnl = ((self.perp_entry_avg - (self._exit_perp_avg or 0.0)) * (self._exit_perp_amount_closed or 0.0))  # short: profite si le prix baisse

        if funding_collected is not None:
            total_pnl = spot_pnl + perp_price_pnl + funding_collected
            pnl_note = f"spot ${spot_pnl:+.4f} + perp price ${perp_price_pnl:+.4f} + funding ${funding_collected:+.4f}"
        else:
            total_pnl = spot_pnl + perp_price_pnl
            pnl_note = f"spot ${spot_pnl:+.4f} + perp price ${perp_price_pnl:+.4f} + funding NON VÉRIFIÉ (à consulter sur l'exchange, non inclus dans ce total)"

        self.logger.info(f"[FundingArb] Hedge closed ({reason}). Real P&L: ${total_pnl:+.4f} [{pnl_note}]")
        await self.notifier.send_message(f"🔵 *Funding Arb Exit ({reason})* 🔵\nReal P&L: ${total_pnl:+.4f}\n({pnl_note})")
        if self.trade_logger:
            spot_amount_closed = self._exit_spot_amount_closed or 0.0
            self.trade_logger.log_trade(
                event_type='FUNDING_ARB_EXIT', platform_sell='Binance', symbol=self.spot_symbol,
                volume=spot_amount_closed, sell_price=(self._exit_spot_proceeds / spot_amount_closed) if spot_amount_closed else 0,
                profit_usd=total_pnl, details=f"reason={reason}, {pnl_note}"
            )
        await self._apply_breaker(total_pnl)
        self._reset_position_state()

    def _reset_position_state(self):
        self.in_position = False
        self.entry_time_ms = None
        self.spot_amount = 0.0
        self.perp_amount = 0.0
        self.spot_entry_cost_usd = 0.0
        self.perp_entry_avg = 0.0
        self.entry_apr_at_open = None
        self._exit_perp_avg = None
        self._exit_perp_amount_closed = None
        self._exit_spot_proceeds = None
        self._exit_spot_amount_closed = None

    async def _apply_breaker(self, pnl_usd):
        self.session_pnl_usd += pnl_usd
        if not self._halted and self.session_pnl_usd <= -self.max_session_loss_usd:
            self._halted = True
            self.logger.critical(f"[FundingArb] CIRCUIT BREAKER TRIGGERED: session P&L ${self.session_pnl_usd:.4f} <= -${self.max_session_loss_usd:.4f}. Trading halted.")
            await self.notifier.send_message(f"🛑 *CIRCUIT BREAKER (Funding Arb)* 🛑\nSession P&L: ${self.session_pnl_usd:.4f}\nTrading has been halted and will NOT resume automatically. Restart the bot after review.")
