import os, re, dotenv
dotenv.load_dotenv()
tok = os.getenv("BOT_TOKEN")
print("TOKEN :", repr(tok))                       # repr() shows hidden chars
print("LENGTH:", len(tok) if tok else "None")
print("FORMAT:", bool(re.fullmatch(r"\d{6,10}:[0-9A-Za-z_-]{35}", tok)))
