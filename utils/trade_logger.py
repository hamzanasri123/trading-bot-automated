# analysis/trade_logger.py
import sqlite3
import logging
import threading
from queue import Queue, Empty

class TradeLogger:
    def __init__(self, db_path='logs/trading_journal.db'):
        self.db_path = db_path
        self.logger = logging.getLogger(self.__class__.__name__)
        self.queue = Queue()
        
        # La connexion à SQLite doit être propre à chaque thread
        self.conn = self._create_connection()
        self._init_db()
        
        self.worker_thread = threading.Thread(target=self._process_queue, daemon=True)
        self.worker_thread.start()
        self.logger.info(f"Journal de trading initialisé. Base de données: {self.db_path}")

    def _create_connection(self):
        """Crée une connexion à la base de données."""
        try:
            # check_same_thread=False est nécessaire car on écrit depuis un thread différent
            return sqlite3.connect(self.db_path, check_same_thread=False)
        except sqlite3.Error as e:
            self.logger.error(f"Erreur de connexion à la base de données SQLite: {e}")
            return None

    def _init_db(self):
        """Crée la table des trades si elle n'existe pas."""
        if not self.conn: return
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    event_type TEXT NOT NULL, -- 'TAKER_EXEC', 'MAKER_FILLED', 'HEDGING_EXEC'
                    platform_buy TEXT,
                    platform_sell TEXT,
                    symbol TEXT,
                    volume REAL,
                    buy_price REAL,
                    sell_price REAL,
                    profit_usd REAL,
                    profit_pct REAL,
                    details TEXT -- Pour stocker des infos supplémentaires (ex: ID d'ordres)
                )
            """)
            self.conn.commit()
        except sqlite3.Error as e:
            self.logger.error(f"Erreur lors de la création de la table 'trades': {e}")

    def _process_queue(self):
        """Une tâche de fond qui écrit les logs dans la base de données."""
        while True:
            try:
                # Attend un nouvel item pendant 1 seconde, puis vérifie si on doit s'arrêter
                item = self.queue.get(timeout=1)
                self._insert_record(item)
                self.queue.task_done()
            except Empty:
                continue
            except Exception as e:
                self.logger.error(f"Erreur dans le worker du TradeLogger: {e}")

    def _insert_record(self, record):
        """Insère un enregistrement dans la base de données."""
        if not self.conn: return
        try:
            cursor = self.conn.cursor()
            columns = ', '.join(record.keys())
            placeholders = ', '.join('?' * len(record))
            sql = f"INSERT INTO trades ({columns}) VALUES ({placeholders})"
            cursor.execute(sql, tuple(record.values()))
            self.conn.commit()
        except sqlite3.Error as e:
            self.logger.error(f"Erreur lors de l'insertion dans la base de données: {e}")

    def log_trade(self, **kwargs):
        """Méthode publique pour ajouter un trade à la file d'attente."""
        self.queue.put(kwargs)

    def close(self):
        """Ferme proprement la connexion à la base de données."""
        if self.conn:
            self.logger.info("Fermeture du journal de trading...")
            # Attend que la file soit vide avant de fermer
            self.queue.join()
            self.conn.close()
