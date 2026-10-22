// Skrip situs gdxgvfk — berkas ini TIDAK diantar oleh
// server webnya, melainkan dari repo GitHub lewat
// jsDelivr. Data katalog pun diambil lewat jsDelivr.
(function () {
  var DOORS = [
    'https://cdn.jsdelivr.net/gh/adiytharpansa/onimeku-data@main/web/catalog-full.json',
    'https://fastly.jsdelivr.net/gh/adiytharpansa/onimeku-data@main/web/catalog-full.json',
    'https://gcore.jsdelivr.net/gh/adiytharpansa/onimeku-data@main/web/catalog-full.json'
  ];
  var app = document.getElementById('app');

  function unwrap(u) {
    if (typeof u !== 'string' || !u) return u;
    try {
      if (u.indexOf('/api/img?') >= 0) {
        var inner = new URL(u).searchParams.get('u');
        if (inner) return unwrap(inner);
      }
    } catch (e) {}
    return u;
  }

  function render(items) {
    var shown = items.slice(0, 60);
    var html = '<header><h1>gdxgvfk</h1><p class="sub">' + items.length + ' judul anime</p></header><div class="grid">';
    shown.forEach(function (t) {
      var poster = unwrap(t.poster) || '';
      var meta = [t.year, (t.score != null && t.score !== 0) ? '★ ' + t.score : null, t.latestEpisode ? t.latestEpisode + ' episode' : null].filter(Boolean).join(' · ');
      html += '<div class="card">' + (poster ? '<img loading="lazy" src="' + poster + '" alt="">' : '') +
        '<div class="t">' + String(t.title || '').replace(/</g, '&lt;') + '</div>' +
        '<div class="m">' + meta + '</div></div>';
    });
    html += '</div><footer>&copy; 2026</footer>';
    app.innerHTML = html;
  }

  function tryDoor(i) {
    if (i >= DOORS.length) {
      app.innerHTML = '<p class="loading">Data belum bisa dimuat.</p>';
      return;
    }
    fetch(DOORS[i])
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (items) {
        if (!Array.isArray(items) || items.length < 500) throw new Error('tidak penuh');
        render(items);
      })
      .catch(function () { tryDoor(i + 1); });
  }
  tryDoor(0);
})();
