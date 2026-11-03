// Theme choice: Light, Dark or System (the default, which follows the OS through CSS,
// including when the OS setting changes). Stores only the chosen word, and works
// without storage for the current page.
(function () {
	var KEY = 'mdv-home-theme';
	var THEMES = ['light', 'dark', 'system'];
	var root = document.documentElement;
	var saved = 'system';
	try {
		var stored = localStorage.getItem(KEY);
		if (THEMES.indexOf(stored) >= 0) saved = stored;
	} catch (_) { }
	function apply(choice) {
		if (choice === 'light' || choice === 'dark') root.setAttribute('data-theme', choice);
		else root.removeAttribute('data-theme');
	}
	apply(saved);
	var radios = document.querySelectorAll('input[name="theme"]');
	for (var i = 0; i < radios.length; i++) {
		radios[i].checked = radios[i].value === saved;
		radios[i].addEventListener('change', function (event) {
			var choice = event.target.value;
			if (!event.target.checked || THEMES.indexOf(choice) < 0) return;
			apply(choice);
			try { localStorage.setItem(KEY, choice); } catch (_) { }
		});
	}
})();

// The two menus (download and theme) are details elements, which open and close
// without script. This adds: a click outside or Escape closes them (Escape returns
// focus to the trigger), choosing a theme or a download with the pointer closes the
// menu, and the arrow keys move through the download links.
(function () {
	var menus = document.querySelectorAll('details.menu');
	function closeAll(except) {
		for (var i = 0; i < menus.length; i++) if (menus[i] !== except) menus[i].open = false;
	}
	document.addEventListener('click', function (event) {
		var menu = event.target.closest('details.menu');
		closeAll(menu);
		// event.detail is 0 for the click a keyboard selection makes, so Space on a
		// theme radio keeps the menu open.
		if (menu && event.detail && event.target.closest('.menu-panel')) menu.open = false;
	});
	document.addEventListener('keydown', function (event) {
		var menu = event.target.closest && event.target.closest('details.menu');
		if (event.key === 'Escape') {
			for (var i = 0; i < menus.length; i++) {
				if (!menus[i].open) continue;
				menus[i].open = false;
				if (menus[i].contains(document.activeElement)) menus[i].querySelector('summary').focus();
			}
			return;
		}
		if (!menu || !menu.classList.contains('download')) return;
		if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
		event.preventDefault();
		menu.open = true;
		var items = menu.querySelectorAll('.menu-panel a');
		var at = Array.prototype.indexOf.call(items, document.activeElement);
		var next = event.key === 'ArrowDown' ? at + 1 : (at < 0 ? items.length : at) - 1;
		items[(next + items.length) % items.length].focus();
	});
})();

// The screenshot fades in when its image arrives (see .frame img.pending).
(function () {
	var shots = document.querySelectorAll('.frame img');
	function shown() { this.classList.remove('pending'); }
	for (var i = 0; i < shots.length; i++) {
		if (shots[i].complete && shots[i].naturalWidth) continue;
		shots[i].classList.add('pending');
		shots[i].addEventListener('load', shown);
		shots[i].addEventListener('error', shown);
	}
})();

// The fish swims down the right margin as the page scrolls down, and back up as it
// scrolls up. One passive scroll listener, at most one update per frame, and only
// transform and class changes. Purely decorative.
(function () {
	var fish = document.querySelector('.fish');
	var reduce = window.matchMedia('(prefers-reduced-motion: reduce)');
	var lastY = window.scrollY;
	var queued = false;
	var idle;
	function place() {
		queued = false;
		var y = window.scrollY;
		var room = document.documentElement.scrollHeight - window.innerHeight;
		var still = reduce.matches;
		var p = still || room <= 0 ? 0 : Math.min(1, Math.max(0, y / room));
		var top = 96;
		var bottom = Math.max(top, window.innerHeight - 136);
		var drift = still ? 0 : Math.sin(p * Math.PI * 3) * 6;
		fish.style.transform = 'translate3d(' + drift.toFixed(1) + 'px,' + Math.round(top + p * (bottom - top)) + 'px,0)';
		if (!still && y !== lastY) {
			fish.classList.toggle('down', y > lastY);
			fish.classList.toggle('up', y < lastY);
			fish.classList.add('swim');
			clearTimeout(idle);
			idle = setTimeout(function () { fish.classList.remove('down', 'up', 'swim'); }, 350);
		}
		lastY = y;
	}
	function schedule() {
		if (!queued) {
			queued = true;
			requestAnimationFrame(place);
		}
	}
	window.addEventListener('scroll', schedule, { passive: true });
	window.addEventListener('resize', schedule);
	place();
	window.addEventListener('load', function () {
		setTimeout(function () { fish.classList.add('in'); }, 300);
	});
})();
