/* DF Tracker site scripts. No cookies, no analytics, no storage. */
(function () {
  // ---- headline number counts up once (home page only)
  var el = document.getElementById('avg');
  if (el && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    var target = +el.dataset.count, start = null;
    el.textContent = '0';
    var tick = function (ts) {
      if (start === null) start = ts;
      var p = Math.min((ts - start) / 1100, 1);
      el.textContent = Math.round(target * (1 - Math.pow(1 - p, 3))).toLocaleString('en-US');
      if (p < 1) requestAnimationFrame(tick);
    };
    setTimeout(function () { requestAnimationFrame(tick); }, 950);
  }

  // ---- screenshot lightbox
  var box = document.getElementById('lightbox');
  if (box && typeof box.showModal === 'function') {
    var img = box.querySelector('img');
    document.querySelectorAll('a.shot').forEach(function (a) {
      a.addEventListener('click', function (e) {
        e.preventDefault();
        img.src = a.getAttribute('href');
        img.alt = a.querySelector('img').alt;
        box.showModal();
      });
    });
    box.addEventListener('click', function (e) { if (e.target === box || e.target === img) box.close(); });
  }

  // ---- home page box: pass the name to the Try it page without putting it in the URL
  var hf = document.getElementById('home-form');
  if (hf) hf.addEventListener('submit', function (e) {
    var v = document.getElementById('home-name').value.trim();
    try { sessionStorage.setItem('df_name', v); } catch (x) {}
    e.preventDefault(); location.href = 'try.html';
  });

  // ---- try-it demo
  var form = document.getElementById('demo-form');
  if (!form) return;
  var API = (window.DF_DEMO_API || '').replace(/\/$/, '');
  var input = document.getElementById('demo-name'), btn = document.getElementById('demo-go');
  var out = document.getElementById('demo-out'), left = document.getElementById('demo-left');
  var busy = false;

  function fmt(n) { return Number(n).toLocaleString('en-US'); }
  function node(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function msg(text, bad) { out.replaceChildren(node('p', 'demo-msg' + (bad ? ' bad' : ''), text)); }
  function setLeft(n, limit) {
    left.replaceChildren();
    if (n === null || n === undefined) return;
    left.append(node('b', '', n + ' of ' + limit), document.createTextNode(' free lookups left today'));
    if (n <= 0) { btn.disabled = true; input.disabled = true; }
  }
  function stat(label, value, sub, color) {
    var d = node('div'); d.append(node('span', '', label));
    var b = node('b', '', value); if (color) b.style.color = color; d.append(b);
    if (sub) d.append(node('small', '', sub));
    return d;
  }
  function render(p, sample, cached) {
    var card = node('div', 'result');
    if (sample) card.append(node('span', 'badge', 'Sample data'));
    card.append(node('h3', '', p.name));
    card.append(node('div', 'lv', 'Operations level ' + p.level_operations + '  |  Warfare level ' + p.level_warfare));
    var g = node('div', 'stats3');
    g.append(stat('K/D', p.kd.toFixed(2), fmt(p.kills) + ' kills, ' + fmt(p.deaths) + ' deaths', 'var(--profit)'));
    g.append(stat('EXTRACTION', p.extraction_rate.toFixed(1) + '%', 'of ' + fmt(p.matches) + ' matches'));
    g.append(stat('RANK POINTS', fmt(p.ranked_points)));
    g.append(stat('MATCHES', fmt(p.matches)));
    g.append(stat('TIME PLAYED', fmt(p.hours_played) + ' h'));
    out.replaceChildren(card);
    var more = node('p', 'demo-msg', 'The app also shows K/D by difficulty, accuracy, knocks, category scores and stash value, and the same lookup works from an in-game hotkey overlay. ');
    var a = node('a', '', 'Get DF Tracker'); a.href = 'download.html'; more.append(a, document.createTextNode('.'));
    card.append(g); out.append(more);
  }

  document.getElementById('demo-sample').addEventListener('click', function () {
    render({ name: 'SamplePlayer', level_operations: 50, level_warfare: 60, kd: 2.31, kills: 4620, deaths: 2000,
             extraction_rate: 54.0, matches: 1180, ranked_points: 6100, hours_played: 412.5 }, true);
  });

  fetch(API + '/v1/demo/status').then(function (r) { return r.json(); }).then(function (j) {
    if (j && j.enabled === false) { msg('The live demo is offline right now. You can still view the sample, or download the app.', true); btn.disabled = true; return; }
    if (j && typeof j.remaining === 'number') setLeft(j.remaining, j.limit);
  }).catch(function () {});

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var name = input.value.trim();
    if (busy || !name) return;
    busy = true; btn.disabled = true;
    out.replaceChildren(node('div', 'skeleton'));
    fetch(API + '/v1/demo/lookup', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                     body: JSON.stringify({ name: name }) })
      .then(function (r) { return r.json().catch(function () { return { ok: false, error: 'unavailable' }; }); })
      .then(function (j) {
        if (typeof j.remaining === 'number') setLeft(j.remaining, j.limit);
        if (j.ok) render(j.player, false, j.cached);
        else msg(j.message || 'Something went wrong. Please try again.', true);
      })
      .catch(function () { msg('Could not reach the demo server. Check your connection and try again.', true); })
      .then(function () { busy = false; if (!input.disabled) btn.disabled = false; });
  });

  // A name typed on the home page is handed over in memory (not the address bar)
  try {
    var q = sessionStorage.getItem('df_name');
    if (q) { sessionStorage.removeItem('df_name'); input.value = q.slice(0, 24); input.focus(); }
  } catch (e) {}
})();
