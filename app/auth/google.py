"""
google.py
---------
Google OAuth 2.0 helpers using authlib.

Usage in main.py:
  from app.auth.google import oauth
  app.add_route("/auth/google", oauth.google.authorize_redirect(...))

The `oauth` object must be initialised once at application startup.
Call `init_oauth(app)` after creating the FastAPI app.
"""

from authlib.integrations.starlette_client import OAuth
from starlette.requests import Request

from app.core.config import settings
from app.core.logging_config import get_logger

logger = get_logger(__name__)

oauth = OAuth()

oauth.register(
    name="google",
    client_id=settings.GOOGLE_CLIENT_ID,
    client_secret=settings.GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


async def redirect_to_google(request: Request):
    """Return an authlib redirect response to Google's consent screen."""
    callback_path = request.app.url_path_for("auth_callback")
    redirect_uri = f"{settings.APP_URL.rstrip('/')}{callback_path}"
    # Behind HF Spaces / reverse proxies the scheme may arrive as http — force https
    # for non-local callback URLs. Local dev stays on plain http.
    if redirect_uri.startswith("http://") and "localhost" not in redirect_uri and "127.0.0.1" not in redirect_uri:
        redirect_uri = "https://" + redirect_uri[len("http://"):]
    return await oauth.google.authorize_redirect(request, redirect_uri, prompt="select_account")


async def handle_callback(request: Request) -> dict | None:
    """
    Exchange the Google callback code for tokens and return user info.

    Returns:
        {"email": str, "name": str, "picture": str} on success, None on failure.
    """
    try:
        token = await oauth.google.authorize_access_token(request)
        user_info = token.get("userinfo") or await oauth.google.userinfo(token=token)
        email = (user_info.get("email") or "").lower().strip()
        name  = user_info.get("name") or email
        if not email:
            logger.warning("Google callback returned no email")
            return None
        return {"email": email, "name": name, "picture": user_info.get("picture", "")}
    except Exception as e:
        logger.warning("Google OAuth callback failed: %s", e)
        return None
