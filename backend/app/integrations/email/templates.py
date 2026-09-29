"""Localized plain-text email templates (TD-20, TD-27). A new UI locale needs an entry here."""

from string import Template

from app.core.enums import SupportedLocale

_PASSWORD_RESET: dict[SupportedLocale, tuple[str, Template]] = {
    SupportedLocale.EN: (
        "Reset your AI Job Assistant password",
        Template(
            "Someone asked to reset the password of your AI Job Assistant account.\n"
            "\n"
            "To choose a new password, open this link within $minutes minutes:\n"
            "$link\n"
            "\n"
            "The link works once. Resetting the password signs you out on all devices.\n"
            "\n"
            "If you didn't ask for this, ignore this email: your password stays the same.\n"
        ),
    ),
    SupportedLocale.RU: (
        "Сброс пароля AI Job Assistant",
        Template(
            "Кто-то запросил сброс пароля для вашего аккаунта AI Job Assistant.\n"
            "\n"
            "Чтобы задать новый пароль, откройте ссылку в течение $minutes мин.:\n"
            "$link\n"
            "\n"
            "Ссылка одноразовая. После сброса пароля вы выйдете из аккаунта на всех "
            "устройствах.\n"
            "\n"
            "Если вы не запрашивали сброс, просто проигнорируйте это письмо: пароль "
            "не изменится.\n"
        ),
    ),
}


def password_reset_email(locale: str, *, link: str, minutes: int) -> tuple[str, str]:
    """(subject, body). Unknown locales fall back to English."""
    try:
        key = SupportedLocale(locale)
    except ValueError:
        key = SupportedLocale.EN
    subject, body = _PASSWORD_RESET[key]
    return subject, body.substitute(link=link, minutes=minutes)
