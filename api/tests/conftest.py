from app.config import load_env

# Let tests find DATABASE_URL etc. from the repo's .env when run locally.
load_env()
