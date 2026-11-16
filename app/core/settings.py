import base64

from cryptography.fernet import Fernet

from app.core.config import Deployment, Settings, environment
from app.models.database import StoredSettings


class SettingsStore:
    def __init__(self, deployment: Deployment, key: bytes, overrides: dict | None = None):
        self.deployment = deployment
        self.fernet = Fernet(base64.urlsafe_b64encode(key))
        self.overrides = (
            environment(Settings.model_fields, {"qbit_password"})
            if overrides is None
            else overrides
        )

    def _load(self, session) -> dict:
        row = session.get(StoredSettings, 1)
        stored = dict(row.values) if row else {}
        if encrypted := stored.get("qbit_password"):
            stored["qbit_password"] = self.fernet.decrypt(encrypted.encode()).decode()
        return stored

    def _validate(self, values: dict) -> Settings:
        values = self.deployment.defaults() | values | self.overrides
        return self.deployment.validate_settings(Settings.model_validate(values))

    def get(self, session) -> Settings:
        return self._validate(self._load(session))

    def update(self, session, updates: dict) -> None:
        stored = self._load(session)
        for key, value in updates.items():
            if key not in Settings.model_fields or key in self.overrides:
                raise ValueError("A supplied setting is unknown or managed by the environment.")
            stored[key] = value
        normalized = self._validate(stored)
        values = normalized.model_dump(mode="json", exclude={"qbit_password"})
        values = {key: values[key] for key in stored if key in values}
        if stored.get("qbit_password"):
            values["qbit_password"] = self.fernet.encrypt(
                normalized.qbit_password.get_secret_value().encode()
            ).decode()
        session.merge(StoredSettings(id=1, values=values))
        session.commit()

    def public(self, session) -> dict:
        settings = self.get(session)
        return {
            **settings.model_dump(mode="json", exclude={"qbit_password"}),
            "qbit_password_set": bool(settings.qbit_password.get_secret_value()),
            "environment_managed": sorted(self.overrides),
        }
