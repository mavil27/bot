# bot
discord music bot

# How to Use
git clone https://github.com/mavil27/bot.git
cd bot
cp config.example.py config.py
nano config.py
- You should change discord token.
- Lavalink password must be changed to the same as in application.yml.

docker compose up -d
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python3 bot.py
