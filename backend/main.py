from fastapi import FastAPI
from routes import players, teams, scoring


app = FastAPI(title="Fantasy CS API")

app.include_router(players.router)
app.include_router(teams.router)
app.include_router(scoring.router)