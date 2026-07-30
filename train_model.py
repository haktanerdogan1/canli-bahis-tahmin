import sqlite3
import pandas as pd
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score, accuracy_score, classification_report
import pickle
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'database', 'fh_goal_predictor.db')
MODEL_PATH = os.path.join(os.path.dirname(__file__), 'models')
os.makedirs(MODEL_PATH, exist_ok=True)

def train_baseline_model():
    print("Loading historical match data from database...")
    conn = sqlite3.connect(DB_PATH)
    
    # Query matches and prematch odds
    query = """
    SELECT 
        m.id,
        m.first_half_home_score,
        m.first_half_away_score,
        p.home_win_odds,
        p.draw_odds,
        p.away_win_odds,
        p.over_25_odds,
        p.under_25_odds
    FROM matches m
    JOIN prematch_odds p ON m.id = p.match_id
    WHERE m.status = 'FINISHED'
    """
    df = pd.read_sql_query(query, conn)
    conn.close()

    if len(df) == 0:
        print("No historical data found for training.")
        return

    print(f"Loaded {len(df)} historical matches.")
    
    # Clean Data
    df = df.dropna()
    
    # Target Variable: Did a goal happen in the first half?
    # y = 1 if (HTHG + HTAG) > 0 else 0
    df['fh_goals'] = df['first_half_home_score'] + df['first_half_away_score']
    df['target'] = (df['fh_goals'] > 0).astype(int)
    
    # Features
    # Since we lack historical minute-by-minute live snapshots, our baseline model
    # will primarily rely on pre-match probabilities for now. 
    # When live data is collected, this model will be retrained with live features (shots, corners, minute).
    
    # Implied Probabilities
    df['prob_home'] = 1 / df['home_win_odds'].replace(0, np.nan)
    df['prob_over25'] = 1 / df['over_25_odds'].replace(0, np.nan)
    df = df.fillna(0) # in case of 0 odds division

    features = ['prob_home', 'prob_over25', 'home_win_odds', 'over_25_odds']
    X = df[features]
    y = df['target']

    # Time-based split (Assuming data is sorted chronologically by default, or we just do simple train_test_split for V1)
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    print("Training Logistic Regression Model (Baseline V1)...")
    model = LogisticRegression(max_iter=1000)
    model.fit(X_train, y_train)

    # Predictions
    y_pred_proba = model.predict_proba(X_test)[:, 1]
    y_pred = model.predict(X_test)

    # Metrics
    brier = brier_score_loss(y_test, y_pred_proba)
    ll = log_loss(y_test, y_pred_proba)
    roc = roc_auc_score(y_test, y_pred_proba)
    
    print("\n--- Model Evaluation ---")
    print(f"Brier Score: {brier:.4f}")
    print(f"Log Loss: {ll:.4f}")
    print(f"ROC-AUC: {roc:.4f}")
    print(f"Accuracy: {accuracy_score(y_test, y_pred):.4f}")
    
    # Save Model
    model_file = os.path.join(MODEL_PATH, 'lr_baseline_v1.pkl')
    with open(model_file, 'wb') as f:
        pickle.dump(model, f)
    
    print(f"\nModel saved to {model_file}")

if __name__ == '__main__':
    train_baseline_model()
