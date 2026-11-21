"""Choix du cookie Codawar à utiliser pour une requête."""


class CookieResolver:
    def __init__(self, store):
        self.store = store

    def any(self):
        """Premier cookie de compte configuré, sinon celui des réglages globaux."""
        for u in self.store.users_with_cookie():
            return u["cookie"]
        return self.store.settings.get("cookie", "")

    def for_owner(self, uid):
        """Cookie du propriétaire d'un calque, sinon un cookie quelconque."""
        u = self.store.users.get(uid) if uid else None
        return (u.get("cookie") if u else "") or self.any()
