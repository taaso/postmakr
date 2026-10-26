/* Handy-Ansicht: Raster, Vollbild zum Wischen, Speichern, Planen, Gepostet.
   Alles ohne Bibliotheken, die Daten kommen aus window.MOBIL und der
   Beitrags-API /api/mobil/beitrag/<kind>/<slug>. */
(function () {
  'use strict';
  var M = window.MOBIL || {};
  var karten = M.karten || [];
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  // ------------------------------------------------------------------ Helfer
  function apiUrl(basis, k) {
    var url = basis.replace('KIND', encodeURIComponent(k.kind)).replace('SLUG', encodeURIComponent(k.slug));
    // Trial Reels: mehrere Termine je Reel, der Termin bestimmt Version und Clip.
    if (k.kind === 'trials' && k.eintrag_id) url += '?eintrag=' + encodeURIComponent(k.eintrag_id);
    return url;
  }
  function isoTag(versatz) {
    var d = new Date();
    d.setDate(d.getDate() + (versatz || 0));
    var m = String(d.getMonth() + 1).padStart(2, '0');
    return d.getFullYear() + '-' + m + '-' + String(d.getDate()).padStart(2, '0');
  }
  function jetztZeit() {
    var d = new Date();
    return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  }
  function datumDe(iso) { return iso ? iso.slice(8, 10) + '.' + iso.slice(5, 7) + '.' : ''; }
  function esc(t) {
    return String(t == null ? '' : t).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  var toastTimer;
  function toast(text, fehler) {
    var t = $('#toast');
    t.textContent = text;
    t.classList.toggle('fehler', !!fehler);
    t.classList.add('an');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.remove('an'); }, fehler ? 4200 : 2800);
  }
  function statusText(s) {
    if (!s) return '';
    var t = s.text || '';
    if (s.datum) t += ' ' + datumDe(s.datum);
    return t;
  }
  function pilleSetzen(el, s) {
    el.className = el.className.replace(/\bm-st-\S+/g, '').trim() + ' m-st-' + s.key;
    el.textContent = statusText(s);
  }

  // ------------------------------------------------ Kacheln: Vorschau laden
  function kachelFrameSkalieren(f) {
    var w = f.parentElement.clientWidth;
    if (w) f.style.transform = 'scale(' + (w / 1080) + ')';
  }
  var kachelBeobachter = new IntersectionObserver(function (eintraege) {
    eintraege.forEach(function (e) {
      var el = e.target;
      if (e.isIntersecting) {
        if (el.dataset.src && !el.getAttribute('src')) {
          el.setAttribute('src', el.dataset.src);
          if (el.tagName === 'IFRAME') kachelFrameSkalieren(el);
        }
        if (el.tagName === 'VIDEO') { var p = el.play(); if (p) p.catch(function () {}); }
      } else if (el.tagName === 'VIDEO') {
        el.pause();
      }
    });
  }, { rootMargin: '300px 0px' });
  $$('.m-thumb iframe, .m-thumb video, .p-thumb iframe, .p-thumb video').forEach(function (el) { kachelBeobachter.observe(el); });
  window.addEventListener('resize', function () {
    $$('.m-thumb iframe[src], .p-thumb iframe[src]').forEach(kachelFrameSkalieren);
    $$('.v-quelle').forEach(quelleEinpassen);
  });

  // --------------------------------------------------------------- Vollbild
  var viewer = $('#viewer');
  var feed = $('#feed');
  var tpl = $('#tpl-post');
  var gebaut = false;
  var aktuell = -1;
  var details = {};          // index -> Beitragsdaten
  var tonAn = true;          // Markensound/Originalton standardmaessig an

  function feedBauen() {
    if (gebaut) return;
    gebaut = true;
    karten.forEach(function (k, i) {
      var node = tpl.content.firstElementChild.cloneNode(true);
      node.dataset.index = i;
      $('.v-titel', node).textContent = k.thema;
      $('.v-format', node).textContent = k.format || '';
      pilleSetzen($('.v-status', node), k.status);
      $('.v-slides', node).innerHTML = '<div class="v-slide"><span class="v-laden">Lädt …</span></div>';
      $('.v-slides', node).addEventListener('scroll', function () { slideWechsel(node); }, { passive: true });
      feed.appendChild(node);
      postBeobachter.observe(node);
    });
  }

  var postBeobachter = new IntersectionObserver(function (eintraege) {
    eintraege.forEach(function (e) {
      if (e.isIntersecting && e.intersectionRatio > 0.6) aktivieren(Number(e.target.dataset.index));
    });
  }, { threshold: [0.6] });

  function postNode(i) { return feed.children[i]; }

  function aktivieren(i) {
    if (i === aktuell) return;
    aktuell = i;
    $$('.v-post video').forEach(function (v) { v.pause(); });
    [i, i + 1, i - 1].forEach(function (j) { if (j >= 0 && j < karten.length) laden(j); });
    videoImBlick(postNode(i));
  }

  function laden(i, erzwingen) {
    if (details[i] && !erzwingen) return Promise.resolve(details[i]);
    var k = karten[i];
    return fetch(apiUrl(M.apiBasis, k), { credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (d) {
        details[i] = d;
        k.status = d.status;
        postFuellen(i, d);
        if (i === aktuell) videoImBlick(postNode(i));
        return d;
      })
      .catch(function () {
        var n = postNode(i);
        if (n) $('.v-slides', n).innerHTML = '<div class="v-slide"><span class="v-laden">Konnte nicht geladen werden.</span></div>';
      });
  }

  function postFuellen(i, d) {
    var n = postNode(i);
    if (!n) return;
    var box = $('.v-slides', n);
    box.innerHTML = '';
    if (!d.slides.length) {
      box.innerHTML = '<div class="v-slide"><span class="v-laden">Keine Medien in diesem Beitrag.</span></div>';
    }
    var hatVideo = false;
    d.slides.forEach(function (s) {
      var slide = document.createElement('div');
      slide.className = 'v-slide';
      if (s.typ === 'bild') {
        var img = document.createElement('img');
        img.src = s.url; img.alt = ''; img.decoding = 'async';
        if (i !== aktuell) img.loading = 'lazy';
        slide.appendChild(img);
      } else if (s.typ === 'video') {
        hatVideo = true;
        var v = document.createElement('video');
        v.src = s.url; v.loop = true; v.playsInline = true; v.setAttribute('playsinline', '');
        v.preload = i === aktuell ? 'auto' : 'metadata';
        v.muted = !tonAn;
        v.addEventListener('click', function () { if (v.paused) abspielen(v); else v.pause(); });
        slide.appendChild(v);
      } else {
        var q = document.createElement('div');
        q.className = 'v-quelle';
        q.dataset.hoehe = s.ratio === '916' ? 1920 : 1350;
        var f = document.createElement('iframe');
        f.setAttribute('sandbox', 'allow-same-origin');
        f.setAttribute('tabindex', '-1');
        f.src = s.url;
        q.appendChild(f);
        var h = document.createElement('span');
        h.className = 'v-quelle-hinweis';
        h.textContent = 'Vorschau, noch nicht gerendert';
        q.appendChild(h);
        slide.appendChild(q);
      }
      box.appendChild(slide);
    });
    $$('.v-quelle', box).forEach(quelleEinpassen);
    $('.v-ton', n).hidden = !hatVideo;
    $('.v-ton', n).classList.toggle('stumm', !tonAn);
    $('.v-titel', n).textContent = d.thema;
    $('.v-format', n).textContent = d.format || '';
    pilleSetzen($('.v-status', n), d.status);
    $('[data-akt="gepostet"]', n).classList.toggle('erledigt', d.status.key === 'gepostet');
    punkteSetzen(n, 0);
    renderHinweis(i, d);
  }

  function quelleEinpassen(q) {
    var slide = q.parentElement;
    if (!slide) return;
    var cs = getComputedStyle(slide);
    var w = slide.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
    var h = slide.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom);
    var hoehe = Number(q.dataset.hoehe) || 1350;
    var s = Math.min(w / 1080, h / hoehe);
    q.style.width = (1080 * s) + 'px';
    q.style.height = (hoehe * s) + 'px';
    var f = $('iframe', q);
    f.style.height = hoehe + 'px';
    f.style.transform = 'scale(' + s + ')';
  }

  function punkteSetzen(n, aktiv) {
    var anzahl = $$('.v-slide', n).length;
    var p = $('.v-punkte', n);
    if (anzahl < 2) { p.innerHTML = ''; return; }
    var html = '';
    for (var j = 0; j < anzahl; j++) html += '<i class="' + (j === aktiv ? 'an' : '') + '"></i>';
    p.innerHTML = html;
  }

  function slideIndex(n) {
    var box = $('.v-slides', n);
    return Math.round(box.scrollLeft / Math.max(1, box.clientWidth));
  }

  var wechselTimer;
  function slideWechsel(n) {
    clearTimeout(wechselTimer);
    wechselTimer = setTimeout(function () {
      punkteSetzen(n, slideIndex(n));
      videoImBlick(n);
    }, 90);
  }

  function abspielen(v) {
    v.muted = !tonAn;
    var p = v.play();
    if (p) p.catch(function () {
      // Browser verbietet Ton ohne Tippen: stumm weiter, Ton-Knopf zeigt es.
      v.muted = true;
      tonAn = false;
      $$('.v-ton').forEach(function (b) { b.classList.add('stumm'); });
      v.play().catch(function () {});
    });
  }

  function videoImBlick(n) {
    if (!n) return;
    var idx = slideIndex(n);
    $$('.v-slide', n).forEach(function (s, j) {
      var v = $('video', s);
      if (!v) return;
      if (j === idx && Number(n.dataset.index) === aktuell && !viewer.hidden) abspielen(v);
      else v.pause();
    });
  }

  function oeffnen(i, ohneVerlauf) {
    feedBauen();
    viewer.hidden = false;
    document.body.classList.add('gesperrt');
    aktuell = -1;
    var n = postNode(i);
    feed.scrollTop = n.offsetTop;
    aktivieren(i);
    if (!ohneVerlauf) history.pushState({ viewer: true }, '');
  }

  function schliessen(ausVerlauf) {
    $$('.v-post video').forEach(function (v) { v.pause(); });
    viewer.hidden = true;
    document.body.classList.remove('gesperrt');
    blattZu();
    if (!ausVerlauf && history.state && history.state.viewer) history.back();
  }

  $('#raster').addEventListener('click', function (e) {
    var k = e.target.closest('.m-kachel, .m-zeile');
    if (k) oeffnen(Number(k.dataset.index));
  });
  $('#v-zu').addEventListener('click', function () { schliessen(false); });
  window.addEventListener('popstate', function () {
    if (!$('#blatt').hidden) { blattZu(); history.pushState({ viewer: true }, ''); return; }
    if (!viewer.hidden) schliessen(true);
  });

  // ------------------------------------------------------ Rendern im Feed
  var renderTimer = {};
  function renderHinweis(i, d) {
    var n = postNode(i);
    var box = $('.v-render', n);
    var laeuft = d.render && (d.render.state === 'queued' || d.render.state === 'running');
    if (!d.fehlen && !laeuft) { box.hidden = true; return; }
    box.hidden = false;
    var text = $('.v-render-text', box);
    box.classList.toggle('laeuft', !!laeuft);
    if (laeuft) {
      text.textContent = 'Wird gerendert … ' + (d.render.percent || 0) + ' %';
      $('.v-balken i', box).style.width = (d.render.percent || 0) + '%';
      renderVerfolgen(i, d.render.job_id);
    } else {
      text.textContent = d.fehlen === 1
        ? 'Ein Slide ist noch nicht gerendert. Zum Speichern einmal rendern.'
        : d.fehlen + ' Slides sind noch nicht gerendert. Zum Speichern einmal rendern.';
    }
    $('.v-render-los', box).onclick = function () { renderStarten(i); };
  }

  function renderStarten(i) {
    var d = details[i];
    if (!d) return;
    var box = $('.v-render', postNode(i));
    box.classList.add('laeuft');
    $('.v-render-text', box).textContent = 'Wird gerendert … 0 %';
    fetch(d.render_url, { method: 'POST', credentials: 'same-origin' })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (a) {
        if (!a.ok || !a.j.ok) throw new Error(a.j.error || 'Rendern konnte nicht starten.');
        renderVerfolgen(i, a.j.job_id);
      })
      .catch(function (err) {
        box.classList.remove('laeuft');
        toast(err.message, true);
        renderHinweis(i, d);
      });
  }

  function renderVerfolgen(i, jobId) {
    if (renderTimer[i]) return;
    var d = details[i];
    renderTimer[i] = setInterval(function () {
      fetch(d.render_status_url + '?job_id=' + encodeURIComponent(jobId), { credentials: 'same-origin' })
        .then(function (r) { return r.json(); })
        .then(function (s) {
          var box = $('.v-render', postNode(i));
          if (s.state === 'done' || s.state === 'error' || s.state === 'idle') {
            clearInterval(renderTimer[i]); renderTimer[i] = null;
            if (s.state === 'error') { toast(s.error || 'Rendern fehlgeschlagen.', true); box.classList.remove('laeuft'); }
            else { toast('Fertig gerendert'); laden(i, true); }
            return;
          }
          $('.v-render-text', box).textContent = 'Wird gerendert … ' + (s.percent || 0) + ' %';
          $('.v-balken i', box).style.width = (s.percent || 0) + '%';
        })
        .catch(function () {});
    }, 2000);
  }

  // ---------------------------------------------------------- Aktionen
  feed.addEventListener('click', function (e) {
    var b = e.target.closest('[data-akt]');
    if (!b) return;
    var n = b.closest('.v-post');
    var i = Number(n.dataset.index);
    var akt = b.dataset.akt;
    if (akt === 'ton') {
      tonAn = !tonAn;
      $$('.v-ton').forEach(function (x) { x.classList.toggle('stumm', !tonAn); });
      $$('.v-post video').forEach(function (v) { v.muted = !tonAn; });
      videoImBlick(n);
      return;
    }
    laden(i).then(function (d) {
      if (!d) return;
      if (akt === 'speichern') blattSpeichern(i, d);
      if (akt === 'planen') blattPlanen(i, d, 'planen');
      if (akt === 'gepostet') blattPlanen(i, d, 'gepostet');
      if (akt === 'text') blattText(d);
    });
  });

  // ------------------------------------------------------------- Blatt
  var blatt = $('#blatt');
  var hinter = $('#s-hinter');
  function blattAuf(titel, html) {
    $('#s-titel').textContent = titel;
    $('#s-inhalt').innerHTML = html;
    blatt.hidden = false;
    hinter.hidden = false;
  }
  function blattZu() { blatt.hidden = true; hinter.hidden = true; }
  hinter.addEventListener('click', blattZu);

  // Speichern: iOS legt Bilder und Videos ueber das Teilen-Menue direkt in
  // die Fotos. Dafuer muessen die Dateien vorher geladen sein, weil das
  // Teilen nur direkt aus dem Tippen heraus erlaubt ist.
  function kannDateienTeilen() {
    try {
      return !!(navigator.canShare && navigator.canShare({ files: [new File(['x'], 'a.png', { type: 'image/png' })] }));
    } catch (e) { return false; }
  }

  function blattSpeichern(i, d) {
    var fertig = d.slides.filter(function (s) { return s.typ !== 'quelle'; });
    var html = '';
    if (d.fehlen) {
      html += '<p class="s-hinweis">' + d.fehlen + (d.fehlen === 1 ? ' Slide ist' : ' Slides sind') +
        ' noch nicht gerendert. Starte das Rendern oben im Beitrag, danach kannst du alles speichern.</p>';
    } else {
      html += '<p class="s-hinweis">' + fertig.length + (fertig.length === 1 ? ' Datei' : ' Dateien') +
        ' in voller Auflösung.' + (fertig.some(function (s) { return s.typ === 'video'; }) && M.markensound && M.markensound.aktiv ? ' Videos kommen mit Markensound.' : '') + '</p>';
    }
    html += '<ul class="s-dateien">';
    fertig.forEach(function (s, j) {
      var mini = s.typ === 'bild'
        ? '<img class="s-mini" src="' + esc(s.url) + '" alt="">'
        : '<video class="s-mini" src="' + esc(s.url) + '#t=0.1" muted playsinline preload="metadata"></video>';
      html += '<li>' + mini + '<span class="s-name">' + (j + 1) + '. ' + esc(s.name) +
        '<br><span class="s-art">' + (s.typ === 'video' ? 'Video' : 'Bild') + '</span></span>' +
        '<a href="' + esc(s.url) + '?download=1" download>Laden</a></li>';
    });
    html += '</ul>';
    if (fertig.length) {
      html += kannDateienTeilen()
        ? '<button class="s-knopf" id="s-teilen" type="button" disabled>Wird vorbereitet …</button>'
        : '<button class="s-knopf" id="s-alle" type="button">Alle herunterladen</button>';
    }
    blattAuf('Speichern', html);

    var alle = $('#s-alle');
    if (alle) alle.onclick = function () {
      fertig.forEach(function (s, j) {
        setTimeout(function () {
          var a = document.createElement('a');
          a.href = s.url + '?download=1'; a.download = '';
          document.body.appendChild(a); a.click(); a.remove();
        }, j * 400);
      });
    };

    var teilen = $('#s-teilen');
    if (!teilen) return;
    var dateien = [];
    var geladen = 0;
    Promise.all(fertig.map(function (s, j) {
      return fetch(s.url, { credentials: 'same-origin' })
        .then(function (r) { if (!r.ok) throw new Error(); return r.blob(); })
        .then(function (b) {
          geladen++;
          teilen.textContent = 'Lade ' + geladen + ' von ' + fertig.length + ' …';
          var endung = s.name.split('.').pop().toLowerCase();
          var typ = b.type || (endung === 'mp4' ? 'video/mp4' : 'image/' + (endung === 'jpg' ? 'jpeg' : endung));
          dateien[j] = new File([b], String(j + 1).padStart(2, '0') + '_' + s.name, { type: typ });
        });
    })).then(function () {
      teilen.disabled = false;
      teilen.textContent = fertig.length === 1 ? 'In Fotos sichern' : 'Alle ' + fertig.length + ' in Fotos sichern';
      teilen.onclick = function () {
        navigator.share({ files: dateien }).then(function () {
          toast('Gespeichert');
        }).catch(function (err) {
          if (err && err.name !== 'AbortError') toast('Teilen ging nicht. Lade die Dateien einzeln.', true);
        });
      };
    }).catch(function () {
      teilen.textContent = 'Laden fehlgeschlagen';
    });
  }

  function kanalOptionen(vorwahl) {
    return (M.kanaele || []).map(function (k) {
      return '<option value="' + esc(k.key) + '"' + (k.key === vorwahl ? ' selected' : '') + '>' + esc(k.name) + '</option>';
    }).join('');
  }

  function blattPlanen(i, d, aktion) {
    var k = karten[i];
    var entwurf = d.kind === 'drafts';
    var vorwahl = (M.kanaele && M.kanaele[0] && M.kanaele[0].key) || '';
    var geplant = (d.eintraege || []).filter(function (e) { return e.status === 'geplant'; });
    if (aktion === 'gepostet' && geplant.length) vorwahl = geplant[0].kanal || vorwahl;
    var html = '';
    if (aktion === 'planen') {
      html += '<p class="s-hinweis">' + (entwurf ? 'Der Entwurf wird dabei freigegeben. ' : '') +
        'Der Termin erinnert dich im Kalender. Nach dem Posten markierst du ihn als gepostet.</p>';
    } else {
      html += '<p class="s-hinweis">' + (entwurf ? 'Der Entwurf wird dabei freigegeben. ' : '') +
        'Trägt den Beitrag als gepostet in den Kalender ein.' + (geplant.length ? ' Der geplante Termin wird dabei umgestellt.' : '') + '</p>';
    }
    html += '<label for="s-kanal">Kanal</label><select id="s-kanal">' + kanalOptionen(vorwahl) + '</select>';
    html += '<div class="s-zeile"><div><label for="s-datum">Datum</label><input id="s-datum" type="date" value="' +
      (aktion === 'planen' ? isoTag(1) : isoTag(0)) + '"></div><div><label for="s-zeit">Uhrzeit</label><input id="s-zeit" type="time" value="' +
      (aktion === 'planen' ? '18:00' : jetztZeit()) + '"></div></div>';
    var knopf = aktion === 'planen'
      ? (entwurf ? 'Freigeben und einplanen' : 'Einplanen')
      : (entwurf ? 'Freigeben und als gepostet markieren' : 'Als gepostet markieren');
    html += '<button class="s-knopf" id="s-los" type="button">' + knopf + '</button>';
    if ((d.eintraege || []).length) {
      html += '<label>Im Kalender</label><ul class="s-eintraege">' + d.eintraege.map(function (e) {
        return '<li><span>' + (e.status === 'gepostet' ? 'Gepostet' : 'Geplant') + ' ' + datumDe(e.datum) + ' ' + esc(e.uhrzeit) +
          (e.kanal ? ', ' + esc(kanalName(e.kanal)) : '') + '</span><button type="button" data-weg="' + esc(e.id) + '">Entfernen</button></li>';
      }).join('') + '</ul>';
    }
    blattAuf(aktion === 'planen' ? 'Einplanen' : 'Als gepostet markieren', html);

    $('#s-los').onclick = function () {
      var btn = this;
      btn.disabled = true;
      fetch(apiUrl(M.statusBasis, k), {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          aktion: aktion, kanal: $('#s-kanal').value,
          datum: $('#s-datum').value, uhrzeit: $('#s-zeit').value
        })
      }).then(function (r) { return r.json(); }).then(function (j) {
        if (!j.ok) throw new Error(j.fehler || 'Das ging nicht.');
        k.kind = j.kind; k.slug = j.slug;
        blattZu();
        var e = j.eintrag;
        if (aktion === 'planen') {
          toast((j.freigegeben ? 'Freigegeben, ' : '') + 'geplant für ' + datumDe(e.datum) + ' um ' + e.uhrzeit);
        } else {
          var karte = karten[i] || {};
          toast(karte.reihe_ende && karte.reihe
            ? 'Gepostet. Das war die letzte Folge von „' + karte.reihe + '“, neue Folgen einplanen.'
            : (j.freigegeben ? 'Freigegeben und ' : '') + 'als gepostet markiert');
        }
        nachAenderung(i);
      }).catch(function (err) {
        btn.disabled = false;
        toast(err.message, true);
      });
    };
    $$('[data-weg]', $('#s-inhalt')).forEach(function (b) {
      b.onclick = function () {
        fetch(M.kalenderBasis.replace('00000000', b.dataset.weg), { method: 'DELETE', credentials: 'same-origin' })
          .then(function (r) { if (!r.ok) throw new Error('Entfernen ging nicht.'); })
          .then(function () { blattZu(); toast('Aus dem Kalender entfernt'); nachAenderung(i); })
          .catch(function (err) { toast(err.message, true); });
      };
    });
  }

  function kanalName(key) {
    var k = (M.kanaele || []).filter(function (x) { return x.key === key; })[0];
    return k ? k.name : key;
  }

  function nachAenderung(i) {
    laden(i, true).then(function (d) {
      if (!d) return;
      var zeile = $('#raster [data-index="' + i + '"]');
      var kachel = zeile && $('.m-pille', zeile);
      if (!kachel) return;
      if (zeile.classList.contains('m-zeile')) {
        // Im Plan steht das Datum schon am Tag, die Pille bleibt kurz.
        pilleSetzen(kachel, { key: d.status.key, text: d.status.text });
        var notiz = $('.p-notiz', zeile);
        if (notiz && d.status.key === 'gepostet') notiz.remove();
      } else {
        pilleSetzen(kachel, d.status);
      }
    });
  }

  function blattText(d) {
    var html = d.caption
      ? '<div class="s-caption">' + esc(d.caption) + '</div><button class="s-knopf" id="s-kopie" type="button">Text kopieren</button>'
      : '<p class="s-hinweis">Dieser Beitrag hat noch keine Caption.</p>';
    blattAuf('Caption', html);
    var k = $('#s-kopie');
    if (k) k.onclick = function () {
      navigator.clipboard.writeText(d.caption).then(function () { toast('Kopiert'); blattZu(); })
        .catch(function () { toast('Kopieren ging nicht. Text gedrückt halten und markieren.', true); });
    };
  }

  // Reihen-Hinweis antippen: springt zum Tag der letzten Folge.
  $$('.p-hinweis[data-ziel]').forEach(function (h) {
    h.addEventListener('click', function () {
      var ziel = document.getElementById('tag-' + h.dataset.ziel);
      if (ziel) ziel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  });

  // Plan: Tagesleiste springt zum Tag und markiert den Tag, der gerade oben steht.
  var leiste = $('#p-leiste');
  if (leiste) {
    leiste.addEventListener('click', function (e) {
      var b = e.target.closest('[data-ziel]');
      if (!b || b.disabled) return;
      var ziel = document.getElementById('tag-' + b.dataset.ziel);
      if (ziel) ziel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
    var tagBeobachter = new IntersectionObserver(function (eintraege) {
      eintraege.forEach(function (e) {
        if (!e.isIntersecting) return;
        var id = e.target.id.replace('tag-', '');
        $$('.p-tagknopf', leiste).forEach(function (b) {
          var an = b.dataset.ziel === id;
          b.classList.toggle('aktiv', an);
          if (an) b.scrollIntoView({ inline: 'center', block: 'nearest', behavior: 'smooth' });
        });
      });
    }, { rootMargin: '-150px 0px -60% 0px' });
    $$('.p-tag').forEach(function (t) { tagBeobachter.observe(t); });
  }

  // Direkt einen Beitrag oeffnen (?post=kind/slug, z. B. von der Detailseite)
  if (M.startPost) {
    var teile = M.startPost.split('/');
    var treffer = karten.findIndex(function (k) { return k.kind === teile[0] && k.slug === teile[1]; });
    if (treffer >= 0) oeffnen(treffer);
  }
})();
