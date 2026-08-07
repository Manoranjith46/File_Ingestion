import base64
import os


os.environ.setdefault(
    "ENCRYPTION_KEY",
    base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii"),
)
