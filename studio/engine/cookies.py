"""Choix du ou des cookies Codawar à utiliser pour une requête."""


class CookieResolver:
    def __init__(self, store):
        self.store = store

    def any(self):
        """Premier cookie de compte configuré, sinon celui des réglages globaux."""
        for u in self.store.users_with_cookie():
            return u["cookie"]
        return self.store.settings.get("cookie", "")

    def for_owner(self, uid):
        """Cookie du propriétaire d'un calque ; "" s'il n'en a pas.

        Jamais de repli sur le cookie d'un autre compte : ses crédits et sa cadence
        seraient consommés à son insu.
        """
        u = self.store.users.get(uid) if uid else None
        return (u.get("cookie") if u else "") or ""

    def for_layer(self, layer):
        """Cookies qui peuvent poser ce calque, dans l'ordre des membres.

        Calque personnel : le cookie de son auteur. Calque de projet : le cookie de
        CHAQUE membre, pour que la pose et la défense consomment les crédits de tout
        le groupe au lieu de ceux d'une seule personne. Liste vide : personne ne peut poser.
        """
        pid = (layer or {}).get("project")
        project = self.store.find_project(pid) if pid else None
        owner = self.for_owner((layer or {}).get("owner"))
        if not project:
            return [owner] if owner else []
        cookies = []
        for uid in project.get("members", []):
            u = self.store.users.get(uid)
            ck = (u or {}).get("cookie")
            if ck and ck not in cookies:
                cookies.append(ck)
        return cookies or ([owner] if owner else [])
