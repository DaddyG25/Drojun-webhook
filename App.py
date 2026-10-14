import os


role = os.getenv("APP_ROLE", "gateway").strip().lower()

if role == "gateway":
    from Gateway import app
elif role == "executor":
    from Executor import app
else:
    raise RuntimeError("APP_ROLE must be gateway or executor")
