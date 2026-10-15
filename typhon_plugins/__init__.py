"""
typhon_plugins — the built-in Typhon plugins, one folder each: Basler,
Prophesee and Simulated cameras, Triggering, Classification, Timing scan and
Cropped export (docs/typhon_plugins_design.md 2.3).

A folder here is a built-in plugin ONLY because council_core.plugin_host.
BUILTIN_PLUGINS names it: any other folder here is refused, not loaded, so a
plugin copied in here cannot skip approval (design 4.1). A plugin from
someone else goes in the plugins folder of the plugin home instead.

ADAPTERS OVER TODAY'S CODE (design 7.2)
While the typhon-plugins branch lives, each plugin is a thin adapter: its
manifest, and a layer that reaches the code that stays where it is in
council_core and frame_camera through re-exports in typhon_plugin_api — the
only Typhon module a plugin imports, built-in or not (a test checks). The
code moves into these folders only at stage P7. At stage P1 every adapter
is empty: the host finds, loads and activates it, and it does nothing yet.

Nothing here is imported to list the plugins: the host reads plugin.json.
"""
