# Blender Datasmith Companion 0.10.0

Free Blender companion for the separately distributed Blender Datasmith Bridge Unreal Editor plugin. This add-on does not work alone and does not include Unreal Engine or Epic Datasmith binaries. The Unreal plugin is a review candidate; a live Fab listing is not yet available.

Download **BlenderDatasmithAddon-0.10.0.zip** from [Releases](https://github.com/eswarankiruba23/blender-datasmith-companion/releases/tag/v0.10.0). Do not install GitHub's generated source-code ZIP as the add-on.

# Blender Datasmith Bridge 0.10.0 setup

Requires Windows x64, a compatible licensed Unreal Engine installation, and Blender on the same PC. The current target is Unreal Engine 5.7.4 and Blender 5.2.0 LTS. This is a review candidate; Fab approval is separate.

1. Install the Unreal plugin through Fab when the listing is approved. For this local review ZIP, copy its BlenderDatasmith folder into your project's Plugins folder while Unreal is closed.
2. Open your existing Unreal project. Enable Blender Datasmith Bridge in Edit > Plugins and restart if prompted. Use the stock engine DatasmithImporter; an older modified project-local importer must be removed after backing it up outside Plugins.
3. Choose Datasmith > Blender Setup. Click Download Blender Add-on. The matching companion is downloaded separately from [the public release page](https://github.com/eswarankiruba23/blender-datasmith-companion/releases/tag/v0.10.0). Download BlenderDatasmithAddon-0.10.0.zip, not GitHub's automatically generated source-code ZIP.
4. In Blender, choose Edit > Preferences > Add-ons > Install from Disk, select that ZIP, and enable Unreal-powered Datasmith Bridge.
5. In the Unreal setup panel, click Use This Unreal Project. In Blender's add-on preferences, click Use connection from Unreal, then Save Preferences.
6. Export with File > Export > Unreal Datasmith. Import the resulting .udatasmith through Unreal's Datasmith > File Import. Keep the exported asset folder beside the file.
7. For native Direct Link, open Blender's Datasmith sidebar, choose baking settings, enable Auto Sync if wanted, then Start Direct Link. In Unreal use Datasmith > Direct Link Import, select the Blender source and enable automatic reimport on its Datasmith scene asset.

The background export/publisher process uses the project you registered. No separate helper project, installer, administrator prompt, or buyer C++ compilation is required for the matching precompiled plugin. Keep the registered project and plugin available. The process loads that project's enabled plugins, so startup time and memory depend on the project. Starting export does not require the receiving editor window to be open; receiving live updates does.

Stop Direct Link before switching projects or updating either component. Register the new project, then reload its connection in Blender. Empty, removed or obsolete helper-project preferences can recover from the registered connection; valid manual project selections are retained. The companion does not download or run software automatically.

The Unreal package contains no Blender ZIP, Epic importer copy, standalone Epic SDK binaries or .uproject. Blender's companion is a separate free download requiring the matching Unreal plugin. Install both halves of version 0.10.0.

Save your .blend after its first export/synchronization to retain persistent IDs. The exporter does not automatically save it. Missing textures cannot be reconstructed. Use a small scene for the first test before a large bake.

# Materials and known limitations

Supported nodes translate directly. Bake complex materials uses Cycles to bake supported surface inputs to textures. Automatic fallback baking can be used for unsupported surface graphs. Blender node groups do not become editable Unreal Material Functions.

Glass is a thin-translucent approximation; cloth uses Unreal cloth shading and translated/baked surface inputs. Identical renderer output is not guaranteed. Volumes, displacement, arbitrary shader mixtures and nested refraction do not have complete parity. Missing image files must be supplied by the customer.

Start at 1024 pixels and 16 samples, increasing settings only where useful. Baking can pause Blender, expand instances and use significant RAM/disk. Cached results can be reused. Direct Link performs scene extraction/baking and import, so large edits are not instantaneous.

Same-PC Windows workflow only. Other operating systems, other engine/Blender versions, Substrate projects and a second physical PC have not been certified. Existing large-house test results belong to the previous runtime, not a new full-house certification of 0.10.0.

Keep the Unreal bridge enabled for stock-importer compatibility, cloth handling and repeated-import protection. For scripted imports use unreal.BlenderDatasmithStockImport.import_scene, or an AssetImportTask with an explicit BlenderDatasmithStockFactory. Raw Datasmith Python import can bypass that protection.

If export reports a missing project, use Unreal > Datasmith > Blender Setup > Use This Unreal Project, then reload the connection in Blender. A successfully enabled Blender add-on alone is insufficient: the Unreal half must be installed and connected. If Direct Link is absent, verify the background publisher is running and both halves use the same matching version. Do not expose bridge ports to the public internet.

Support: https://github.com/eswarankiruba23/blender-datasmith-companion/issues. Original companion code: MIT license.
