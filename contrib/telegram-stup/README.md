# Alexys STUP compatibility plugin

Install the `alexys-telegram-stup` directory in the desired Hermes profile's
plugin directory and enable it with `hermes plugins enable alexys-telegram-stup`.
Keep the installed `stup` skill and its existing auto-load setting. The plugin
opts in only when `display.platforms.telegram.stup_updates` is true.

The existing gateway task keeps Telegram's route, message edit behavior,
ownership checks and cancellation. The adapter supplies its French STUP format
and a 120-second interval only on the opted-in Telegram path. Successive points
update one message according to Hermes' default behavior. Runtime data comes from
the current execution middleware payload. Missing fields stay unconfirmed.
Observed tool outcomes provide bounded progress notes without exposing raw
messages or tool results.

The compatibility adapter currently recognizes the native timer on
`bf0203a794`. It refuses a changed implementation and leaves the stock timer
operational. It does not modify or install a fork of Hermes. For rollback,
disable the plugin with `hermes plugins disable alexys-telegram-stup`, then
gracefully restart the gateway to unload the in-process adapter. A supported
upstream heartbeat formatter would replace the compatibility adapter.

Run `python -m pytest contrib/telegram-stup/test_telegram_stup.py` with Hermes
and its test dependencies on the import path. These tests exercise the official
timer with fake time and adapters. Real Telegram delivery requires a separate
live check.
