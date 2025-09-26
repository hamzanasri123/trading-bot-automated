# analysis/performance_analyzer.py
import sqlite3
import pandas as pd
import os

def analyze_performance(db_path='logs/trading_journal.db'):
    """
    Analyse les trades enregistrés dans la base de données SQLite et affiche un rapport.
    """
    print("--- Rapport de Performance du Bot de Trading ---")

    if not os.path.exists(db_path):
        print(f"Erreur: La base de données '{db_path}' n'a pas été trouvée.")
        print("Assurez-vous que le bot a déjà tourné et enregistré des trades.")
        return

    try:
        conn = sqlite3.connect(db_path)
        # Utilise pandas pour lire facilement les données SQL dans un DataFrame
        df = pd.read_sql_query("SELECT * FROM trades", conn)
        conn.close()
    except Exception as e:
        print(f"Erreur lors de la lecture de la base de données: {e}")
        return

    if df.empty:
        print("Aucun trade n'a été trouvé dans le journal.")
        print("Le bot n'a peut-être pas encore exécuté de transaction.")
        return

    # --- Calculs des Indicateurs de Performance ---

    total_trades = len(df)
    
    # On ne considère que les trades où un profit a été réellement calculé
    profitable_trades = df[df['profit_usd'] > 0]
    losing_trades = df[df['profit_usd'] <= 0]

    total_profit_usd = df['profit_usd'].sum()
    win_rate = (len(profitable_trades) / total_trades) * 100 if total_trades > 0 else 0
    
    avg_profit_per_trade = total_profit_usd / total_trades if total_trades > 0 else 0
    avg_profit_winning_trade = profitable_trades['profit_usd'].mean() if len(profitable_trades) > 0 else 0
    avg_loss_losing_trade = losing_trades['profit_usd'].mean() if len(losing_trades) > 0 else 0

    # Profit Factor: Gain total des trades gagnants / Perte totale des trades perdants
    total_gains = profitable_trades['profit_usd'].sum()
    total_losses = abs(losing_trades['profit_usd'].sum())
    profit_factor = total_gains / total_losses if total_losses > 0 else float('inf')

    # --- Affichage du Rapport ---

    print("\n--- Résumé Général ---")
    print(f"Nombre total de trades enregistrés: {total_trades}")
    print(f"Profit/Perte Net Total (estimé):   ${total_profit_usd:,.2f}")
    
    print("\n--- Performance des Trades ---")
    print(f"Taux de réussite (Win Rate):         {win_rate:.2f}%")
    print(f"Nombre de trades gagnants:           {len(profitable_trades)}")
    print(f"Nombre de trades perdants/nuls:      {len(losing_trades)}")
    print(f"Profit Factor:                       {profit_factor:.2f}")

    print("\n--- Statistiques par Trade ---")
    print(f"Gain moyen par trade:                ${avg_profit_per_trade:,.4f}")
    print(f"Gain moyen par trade gagnant:        ${avg_profit_winning_trade:,.4f}")
    print(f"Perte moyenne par trade perdant:     ${avg_loss_losing_trade:,.4f}")

    # Afficher les 5 derniers trades pour un aperçu rapide
    print("\n--- 5 Derniers Trades Enregistrés ---")
    # Affiche les colonnes pertinentes des 5 dernières lignes
    print(df[['timestamp', 'event_type', 'symbol', 'volume', 'profit_usd']].tail(5).to_string(index=False))


if __name__ == "__main__":
    # Pour lancer ce script, assurez-vous d'être dans le bon environnement
    # et que le chemin vers la base de données est correct.
    analyze_performance()
