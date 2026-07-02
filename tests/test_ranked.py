import os
import pandas as pd
from sqlalchemy import create_engine

DB_NAME = os.getenv("HALO_DB_NAME", "halodb")
DB_USER = os.getenv("HALO_DB_USER", "postgres")
DB_PASSWORD = os.getenv("HALO_DB_PASSWORD", "your_password_here")
DB_HOST = os.getenv("HALO_DB_HOST", "localhost")
DB_PORT = os.getenv("HALO_DB_PORT", "5432")

def main():
    db_url = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    engine = create_engine(db_url)

    # Load data
    df = pd.read_sql_query("SELECT * FROM halo_match_stats LIMIT 5", engine)
    print("Columns:", df.columns.tolist())
    print("\nSample data:")
    print(df[["player_gamertag", "playlist", "date"]].head())

    # Check for Ranked Arena
    if "playlist" in df.columns:
        ranked = df[df["playlist"].str.contains("Ranked", case=False, na=False)]
        print(f"\n\nFound {len(ranked)} Ranked matches")
        print("Unique playlists:", df["playlist"].unique())


if __name__ == "__main__":
    main()
