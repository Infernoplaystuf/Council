"""
The plugin contract suites (docs/typhon_plugins_design.md 9.1): one suite per
plugin kind, that every plugin of the kind must pass, run first against the
Simulated plugin (the reference plugin). The camera kind's is here from
stage P2; the trigger-source and feature kinds' join at P3, and the suites
ship with the plugin API as `python -m typhon_plugin_api.check` at P5.
"""
