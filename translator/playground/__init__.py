"""
playground/ — Model comparison, AI judging and interactive chat.

Everything here is off the translation path. It reuses the same prompt
builders and the same HTTP client, so what a model does here is what it would
do in a real run, but it writes no spreadsheet and touches no vault.

  inputs.py  the pasted-lines parser (numbered or plain, auto-detected)
  prefs.py   what to offer by default next time — the sticky judge model
  runner.py  fan-out to several models at once, and the judge call
"""
