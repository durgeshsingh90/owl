/**
 * OWL Patterns mode for draw.io.
 *
 * Adds a Shapes | Patterns switch at the top of the left panel. Shapes is draw.io's usual
 * library. Patterns replaces it with one multi-select dropdown per category in
 * catalog.json (Data Centers, AWS, Azure): the chosen items appear below as shapes to
 * drag or click onto the diagram. The mode and the choices are remembered in this browser.
 */
Draw.loadPlugin(function(ui)
{
	var BASE = 'plugins/owl/';
	var STORE = 'owl.patterns';
	var TABS_HEIGHT = 36;
	var sidebar = ui.sidebar;
	var container = sidebar.container;

	function load(key, fallback)
	{
		try
		{
			var value = JSON.parse(localStorage.getItem(STORE + '.' + key));
			return (value != null) ? value : fallback;
		}
		catch (e)
		{
			return fallback;
		}
	}

	function save(key, value)
	{
		try
		{
			localStorage.setItem(STORE + '.' + key, JSON.stringify(value));
		}
		catch (e)
		{
			// Private window or blocked storage: choices last for this page only.
		}
	}

	function el(tag, className, text)
	{
		var node = document.createElement(tag);

		if (className)
		{
			node.className = className;
		}

		if (text != null)
		{
			node.textContent = text;
		}

		return node;
	}

	function getJson(name)
	{
		return fetch(BASE + name, {cache: 'no-store'}).then(function(response)
		{
			if (!response.ok)
			{
				throw new Error(name + ': HTTP ' + response.status);
			}

			return response.json();
		});
	}

	// Styles

	var css = el('style');
	css.textContent = [
		'.owlTabs{position:absolute;top:0;left:0;right:0;height:' + TABS_HEIGHT + 'px;display:flex;gap:4px;padding:6px 8px;box-sizing:border-box;border-bottom:1px solid var(--owl-line);z-index:2;background:inherit}',
		'.owlTabs button{flex:1;border:1px solid var(--owl-line);background:transparent;color:inherit;border-radius:6px;font:12px system-ui,sans-serif;cursor:pointer;padding:0}',
		'.owlTabs button.owlOn{background:#0066CC;border-color:#0066CC;color:#fff}',
		'.owlPanel{position:absolute;top:' + TABS_HEIGHT + 'px;left:0;right:0;bottom:0;overflow:hidden auto;padding:8px;box-sizing:border-box;font:12px system-ui,sans-serif}',
		'.owlPick{position:relative;margin-bottom:8px}',
		'.owlPick>label{display:block;font-weight:600;margin:0 0 3px 2px}',
		'.owlPickBtn{width:100%;display:flex;align-items:center;justify-content:space-between;gap:6px;border:1px solid var(--owl-line);border-radius:6px;background:transparent;color:inherit;padding:6px 8px;font:inherit;cursor:pointer;text-align:left}',
		'.owlPickBtn span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
		'.owlPickBtn:after{content:"\\25BE";opacity:.6}',
		'.owlMenu{position:absolute;left:0;right:0;top:100%;margin-top:2px;z-index:10;border:1px solid var(--owl-line);border-radius:6px;background:var(--owl-bg);box-shadow:0 6px 18px rgba(0,0,0,.18);display:flex;flex-direction:column;max-height:320px}',
		'.owlMenu input[type=search]{margin:6px;padding:5px 7px;border:1px solid var(--owl-line);border-radius:5px;background:transparent;color:inherit;font:inherit}',
		'.owlMenuBar{display:flex;gap:10px;padding:0 8px 4px}',
		'.owlMenuBar a{color:#0066CC;cursor:pointer}',
		'.owlMenuList{overflow:auto;padding:2px 0 6px}',
		'.owlMenuList label{display:flex;align-items:center;gap:6px;padding:3px 8px;cursor:pointer}',
		'.owlMenuList label:hover{background:var(--owl-hover)}',
		'.owlMenuList small{opacity:.55;margin-left:auto;padding-left:6px;white-space:nowrap}',
		'.owlEmpty{opacity:.6;padding:6px 8px}',
		'.owlSection{margin-top:10px}',
		'.owlSection h3{font:600 11px system-ui,sans-serif;text-transform:uppercase;letter-spacing:.04em;opacity:.7;margin:0 0 4px 2px}',
		'.owlShapes{display:flex;flex-wrap:wrap}',
		'.owlHint{opacity:.65;line-height:1.4;padding:8px 2px;white-space:normal}',
		'.owlError{color:#C9372C;padding:8px 2px;white-space:normal}',
		'.geEditor{--owl-line:rgba(127,127,127,.35);--owl-bg:#fff;--owl-hover:rgba(0,102,204,.08)}',
		'.geEditor.geDarkMode{--owl-bg:#2a2a2a;--owl-hover:rgba(80,160,255,.15)}'
	].join('\n');
	document.head.appendChild(css);

	// Layout: the switch sits above both modes; Shapes keeps draw.io's own panel.

	var tabs = el('div', 'owlTabs');
	var shapesTab = el('button', null, mxResources.get('shapes') || 'Shapes');
	var patternsTab = el('button', null, 'Patterns');
	shapesTab.type = patternsTab.type = 'button';
	tabs.appendChild(shapesTab);
	tabs.appendChild(patternsTab);

	var panel = el('div', 'owlPanel');
	panel.appendChild(el('div', 'owlHint', 'Loading patterns…'));

	container.appendChild(tabs);
	container.appendChild(panel);

	function nativeParts()
	{
		var parts = [];

		for (var i = 0; i < container.children.length; i++)
		{
			var child = container.children[i];

			if (child != tabs && child != panel)
			{
				parts.push(child);
			}
		}

		return parts;
	}

	var mode = (urlParams['owl-mode'] == 'shapes' || urlParams['owl-mode'] == 'patterns') ?
		urlParams['owl-mode'] : load('mode', 'patterns');

	function setMode(value)
	{
		mode = value;
		save('mode', mode);
		shapesTab.classList.toggle('owlOn', mode == 'shapes');
		patternsTab.classList.toggle('owlOn', mode == 'patterns');
		panel.style.display = (mode == 'patterns') ? '' : 'none';

		nativeParts().forEach(function(part)
		{
			part.style.display = (mode == 'patterns') ? 'none' : '';

			// The shape list is the first child: make room for the switch above it.
			if (part.classList.contains('geSidebarFooter'))
			{
				return;
			}

			part.style.marginTop = TABS_HEIGHT + 'px';
			part.style.maxHeight = 'calc(100% - 44px - ' + TABS_HEIGHT + 'px)';
		});
	}

	shapesTab.addEventListener('click', function() { setMode('shapes'); });
	patternsTab.addEventListener('click', function() { setMode('patterns'); });
	setMode(mode);

	// Patterns

	var selection = load('selection', {});
	var openMenu = null;

	function closeMenu()
	{
		if (openMenu != null)
		{
			openMenu.remove();
			openMenu = null;
		}
	}

	document.addEventListener('mousedown', function(event)
	{
		if (openMenu != null && !openMenu.parentNode.contains(event.target))
		{
			closeMenu();
		}
	}, true);

	document.addEventListener('keydown', function(event)
	{
		if (event.key == 'Escape')
		{
			closeMenu();
		}
	});

	function resolve(catalog, indexes)
	{
		var byKey = {};

		Object.keys(indexes).forEach(function(library)
		{
			indexes[library].forEach(function(shape)
			{
				byKey[library + ':' + shape.group + '/' + shape.title] = shape;
			});
		});

		catalog.categories.forEach(function(category)
		{
			category.items.forEach(function(item)
			{
				item.resolved = byKey[(item.library || category.library) + ':' + item.shape] || null;

				if (item.resolved == null && item.style == null && window.console != null)
				{
					console.warn('OWL Patterns: no shape "' + item.shape + '" for ' + item.label);
				}
			});
		});
	}

	function chosen(category)
	{
		var ids = selection[category.id] || [];

		return category.items.filter(function(item)
		{
			return ids.indexOf(item.id) >= 0;
		});
	}

	function summary(category)
	{
		var items = chosen(category);

		if (items.length == 0)
		{
			return 'None selected';
		}
		else if (items.length == category.items.length)
		{
			return 'All (' + items.length + ')';
		}
		else if (items.length <= 2)
		{
			return items.map(function(item) { return item.label; }).join(', ');
		}

		return items.length + ' selected';
	}

	var sections = el('div');

	function render(catalog)
	{
		closeMenu();
		panel.innerHTML = '';

		catalog.categories.forEach(function(category)
		{
			var pick = el('div', 'owlPick');
			pick.appendChild(el('label', null, category.label));
			var button = el('button', 'owlPickBtn');
			button.type = 'button';
			button.appendChild(el('span', null, summary(category)));
			pick.appendChild(button);
			panel.appendChild(pick);

			button.addEventListener('click', function()
			{
				var wasOpen = openMenu != null && openMenu.parentNode == pick;
				closeMenu();

				if (!wasOpen)
				{
					showMenu(catalog, category, pick, button);
				}
			});
		});

		panel.appendChild(sections);
		renderSections(catalog);
	}

	function renderSections(catalog)
	{
		sections.innerHTML = '';
		var any = false;

		catalog.categories.forEach(function(category)
		{
			var items = chosen(category);

			if (items.length == 0)
			{
				return;
			}

			any = true;
			var section = el('div', 'owlSection');
			section.appendChild(el('h3', null, category.label));
			var shapes = el('div', 'owlShapes geSidebar');

			items.forEach(function(item)
			{
				var shape = item.resolved;
				var style = item.style || (shape && shape.style);

				if (style == null)
				{
					return;
				}

				// Containers (data centers, accounts, VPCs) are drawn big and labelled with
				// the item's name; icons keep their usual size with the name underneath.
				var w = item.w || (shape && shape.w) || 60;
				var h = item.h || (shape && shape.h) || 60;
				style = style.replace(/;?$/, ';');
				shapes.appendChild(sidebar.createVertexTemplate(style + 'owlPattern=' +
					category.id + '/' + item.id + ';', w, h, item.label, item.label, true));
			});

			section.appendChild(shapes);
			sections.appendChild(section);
		});

		if (!any)
		{
			sections.appendChild(el('div', 'owlHint',
				'Choose data centers, AWS and Azure resources above. They appear here to drag onto the diagram.'));
		}
	}

	function showMenu(catalog, category, pick, button)
	{
		var menu = el('div', 'owlMenu');
		var search = el('input');
		search.type = 'search';
		search.placeholder = 'Search ' + category.label;
		menu.appendChild(search);

		var bar = el('div', 'owlMenuBar');
		var all = el('a', null, 'Select all');
		var none = el('a', null, 'Clear');
		bar.appendChild(all);
		bar.appendChild(none);
		menu.appendChild(bar);

		var list = el('div', 'owlMenuList');
		menu.appendChild(list);

		function commit(ids)
		{
			selection[category.id] = ids;
			save('selection', selection);
			button.firstChild.textContent = summary(category);
			fill();
			renderSections(catalog);
		}

		function fill()
		{
			var term = search.value.trim().toLowerCase();
			var ids = selection[category.id] || [];
			list.innerHTML = '';

			var items = category.items.filter(function(item)
			{
				return term == '' || item.label.toLowerCase().indexOf(term) >= 0 ||
					(item.shape || '').toLowerCase().indexOf(term) >= 0;
			});

			if (items.length == 0)
			{
				list.appendChild(el('div', 'owlEmpty', 'No matches'));
			}

			items.forEach(function(item)
			{
				var row = el('label');
				var box = el('input');
				box.type = 'checkbox';
				box.checked = ids.indexOf(item.id) >= 0;
				row.appendChild(box);
				row.appendChild(el('span', null, item.label));

				if (item.resolved == null && item.style == null)
				{
					row.appendChild(el('small', null, 'no shape'));
				}

				box.addEventListener('change', function()
				{
					var next = (selection[category.id] || []).filter(function(id) { return id != item.id; });

					if (box.checked)
					{
						next.push(item.id);
					}

					commit(next);
				});

				list.appendChild(row);
			});
		}

		all.addEventListener('click', function()
		{
			commit(category.items.map(function(item) { return item.id; }));
		});

		none.addEventListener('click', function()
		{
			commit([]);
		});

		search.addEventListener('input', fill);
		fill();
		pick.appendChild(menu);
		openMenu = menu;
		search.focus();
	}

	Promise.all([getJson('catalog.json'), getJson('shapes-aws.json'), getJson('shapes-azure.json')])
		.then(function(results)
		{
			var catalog = results[0];
			resolve(catalog, {aws: results[1], azure: results[2]});
			render(catalog);
		})
		.catch(function(error)
		{
			panel.innerHTML = '';
			panel.appendChild(el('div', 'owlError', 'Patterns could not load: ' + error.message));
		});
});
