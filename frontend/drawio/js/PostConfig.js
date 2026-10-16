/**
 * Copyright (c) 2006-2024, JGraph Holdings Ltd
 * Copyright (c) 2006-2024, draw.io AG
 */
// null'ing of global vars need to be after init.js
window.ICONSEARCH_PATH = null;
window.ICON_SERVICE_PATH = null;
// OWL: load the Patterns mode plugin (plugins/owl/patterns.js) on every start.
App.pluginRegistry['owlpatterns'] = 'plugins/owl/patterns.js';
urlParams['p'] = ((urlParams['p'] != null) ? urlParams['p'] + ';' : '') + 'owlpatterns';
