import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'database', 'fh_goal_predictor.db')

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 1. matches
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_match_id TEXT UNIQUE,
            league_id TEXT,
            season TEXT,
            home_team_id TEXT,
            away_team_id TEXT,
            kickoff_time TIMESTAMP,
            status TEXT,
            home_score INTEGER DEFAULT 0,
            away_score INTEGER DEFAULT 0,
            first_half_home_score INTEGER,
            first_half_away_score INTEGER,
            minute INTEGER DEFAULT 0,
            league_name TEXT DEFAULT '',
            league_ccode TEXT DEFAULT '',
            league_logo TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 2. prematch_odds
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS prematch_odds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER,
            captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            home_win_odds REAL,
            draw_odds REAL,
            away_win_odds REAL,
            fh_home_odds REAL,
            fh_draw_odds REAL,
            fh_away_odds REAL,
            fh_over_05_odds REAL,
            fh_under_05_odds REAL,
            fh_over_15_odds REAL,
            fh_under_15_odds REAL,
            over_25_odds REAL,
            under_25_odds REAL,
            btts_yes_odds REAL,
            btts_no_odds REAL,
            FOREIGN KEY(match_id) REFERENCES matches(id)
        )
    ''')

    # 3. live_snapshots
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS live_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER,
            captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            minute INTEGER,
            period TEXT,
            home_score INTEGER,
            away_score INTEGER,
            home_shots INTEGER,
            away_shots INTEGER,
            home_shots_on_target INTEGER,
            away_shots_on_target INTEGER,
            home_shots_off_target INTEGER,
            away_shots_off_target INTEGER,
            home_dangerous_attacks INTEGER,
            away_dangerous_attacks INTEGER,
            home_attacks INTEGER,
            away_attacks INTEGER,
            home_corners INTEGER,
            away_corners INTEGER,
            home_possession INTEGER,
            away_possession INTEGER,
            home_red_cards INTEGER,
            away_red_cards INTEGER,
            home_xg REAL,
            away_xg REAL,
            home_big_chances INTEGER,
            away_big_chances INTEGER,
            FOREIGN KEY(match_id) REFERENCES matches(id)
        )
    ''')

    # 4. team_match_history
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS team_match_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id TEXT,
            match_id INTEGER,
            opponent_team_id TEXT,
            league_id TEXT,
            match_date TIMESTAMP,
            is_home BOOLEAN,
            fh_goals_scored INTEGER,
            fh_goals_conceded INTEGER,
            ft_goals_scored INTEGER,
            ft_goals_conceded INTEGER,
            opponent_strength REAL,
            FOREIGN KEY(match_id) REFERENCES matches(id)
        )
    ''')

    # 5. team_form_features
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS team_form_features (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id TEXT,
            calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_5_fh_goal_rate REAL,
            last_10_fh_goal_rate REAL,
            last_20_fh_goal_rate REAL,
            last_10_fh_scored_rate REAL,
            last_10_fh_conceded_rate REAL,
            home_last_10_fh_goal_rate REAL,
            away_last_10_fh_goal_rate REAL,
            weighted_fh_goal_rate REAL
        )
    ''')

    # 6. bot_predictions
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bot_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER,
            snapshot_id INTEGER,
            bot_name TEXT,
            bot_version TEXT,
            decision TEXT,
            probability REAL,
            confidence TEXT,
            data_quality REAL,
            reasons_json TEXT,
            warnings_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(match_id) REFERENCES matches(id),
            FOREIGN KEY(snapshot_id) REFERENCES live_snapshots(id)
        )
    ''')

    # 7. consensus_predictions
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS consensus_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER,
            snapshot_id INTEGER,
            consensus_version TEXT,
            positive_bot_count INTEGER,
            negative_bot_count INTEGER,
            weighted_probability REAL,
            signal_level TEXT,
            decision TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            outcome TEXT,
            settled_at TIMESTAMP,
            goal_minute INTEGER,
            FOREIGN KEY(match_id) REFERENCES matches(id),
            FOREIGN KEY(snapshot_id) REFERENCES live_snapshots(id)
        )
    ''')

    # 8. bot_metrics
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bot_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_name TEXT,
            bot_version TEXT,
            evaluation_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            sample_size INTEGER,
            won INTEGER,
            lost INTEGER,
            win_rate REAL,
            brier_score REAL,
            log_loss REAL,
            calibration_error REAL,
            precision REAL,
            recall REAL
        )
    ''')

    conn.commit()
    conn.close()
    print(f"Database initialized at {DB_PATH}")

if __name__ == '__main__':
    init_db()
