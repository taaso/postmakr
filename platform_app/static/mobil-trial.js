/* Trial Reels auf dem Handy: Versionen umschalten, Clips
   hochladen und loeschen, Texte kopieren und direkt im Browser aufnehmen,
   mit dem O-Ton der Version als Teleprompter. Die Kamera gibt es nur ueber
   HTTPS oder localhost, sonst bleiben Kamera-App und Mediathek. */
(function () {
  'use strict';
  var T = window.TRIAL || {};
  var $ = function (id) { return document.getElementById(id); };

  // ---- Kleinkram ----------------------------------------------------------
  var toastTimer;
  function toast(text, fehler) {
    var el = $('toast');
    el.textContent = text;
    el.classList.toggle('fehler', !!fehler);
    el.classList.add('an');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.classList.remove('an'); }, fehler ? 5000 : 2200);
  }
  function lesen(key, std) {
    try { var w = localStorage.getItem(key); return w === null ? std : w; } catch (e) { return std; }
  }
  function merken(key, wert) {
    try { localStorage.setItem(key, wert); } catch (e) { /* egal */ }
  }
  function sektion(nr) { return document.querySelector('.t-version[data-version="' + nr + '"]'); }
  function neuLaden(nr) {
    var url = location.pathname + '?v=' + nr;
    location.replace(url);
  }

  // ---- Versionen umschalten ----------------------------------------------
  document.querySelectorAll('[data-zeige-version]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var nr = btn.dataset.zeigeVersion;
      document.querySelectorAll('[data-zeige-version]').forEach(function (b) {
        b.setAttribute('aria-selected', b === btn ? 'true' : 'false');
      });
      document.querySelectorAll('.t-version').forEach(function (s) { s.hidden = s.dataset.version !== nr; });
      history.replaceState(null, '', location.pathname + '?v=' + nr);
      window.scrollTo({ top: 0 });
    });
  });

  // ---- Hochladen ----------------------------------------------------------
  function hochladen(nr, dateien, balken, fertig) {
    var s = sektion(nr);
    var fd = new FormData();
    dateien.forEach(function (d) { fd.append('dateien', d.datei, d.name); });
    var xhr = new XMLHttpRequest();
    xhr.open('POST', s.dataset.hochladen);
    balken.hidden = false;
    balken.firstElementChild.style.width = '0%';
    xhr.upload.onprogress = function (e) {
      if (e.lengthComputable) balken.firstElementChild.style.width = Math.round(e.loaded / e.total * 100) + '%';
    };
    xhr.onload = function () {
      balken.hidden = true;
      var j;
      try { j = JSON.parse(xhr.responseText); } catch (e) { j = { ok: false, error: 'Upload fehlgeschlagen (' + xhr.status + ')' }; }
      if (!j.ok) { toast(j.error, true); if (fertig) fertig(false); return; }
      toast('Hochgeladen');
      if (fertig) fertig(true);
    };
    xhr.onerror = function () {
      balken.hidden = true;
      toast('Upload abgebrochen, Verbindung prüfen.', true);
      if (fertig) fertig(false);
    };
    xhr.send(fd);
  }

  document.querySelectorAll('.t-version').forEach(function (s) {
    var nr = s.dataset.version;
    s.querySelectorAll('[data-datei-input]').forEach(function (input) {
      input.addEventListener('change', function () {
        var dateien = Array.prototype.map.call(input.files, function (f) { return { datei: f, name: f.name }; });
        input.value = '';
        if (!dateien.length) return;
        hochladen(nr, dateien, s.querySelector('[data-balken]'), function (ok) { if (ok) neuLaden(nr); });
      });
    });
    s.querySelectorAll('[data-loeschen]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        // Zweimal tippen statt Browser-Dialog.
        if (!btn.classList.contains('sicher')) {
          btn.classList.add('sicher');
          toast('Nochmal tippen zum Löschen');
          setTimeout(function () { btn.classList.remove('sicher'); }, 2500);
          return;
        }
        fetch(s.dataset.loeschenUrl, { method: 'POST', body: new URLSearchParams({ name: btn.dataset.loeschen }) })
          .then(function (r) { return r.json(); })
          .then(function (j) { if (!j.ok) throw new Error(j.error || 'Fehler'); neuLaden(nr); })
          .catch(function (e) { toast(e.message, true); });
      });
    });
    // Clip in der Flaeche zeigen
    var buehne = s.querySelector('.t-buehne');
    function dateiUrl(name, stand) {
      return s.dataset.dateiUrl.replace('NAME', encodeURIComponent(name)) + (stand ? '?stand=' + stand : '');
    }
    function zeigen(btn) {
      var url = dateiUrl(btn.dataset.zeigen, btn.dataset.stand);
      buehne.innerHTML = btn.dataset.video === '1'
        ? '<video controls playsinline preload="metadata"></video>' : '<img alt="">';
      buehne.firstChild.src = url;
      s.querySelectorAll('[data-zeigen]').forEach(function (b) { b.setAttribute('aria-current', b === btn ? 'true' : 'false'); });
    }
    s.querySelectorAll('[data-zeigen]').forEach(function (btn) {
      btn.addEventListener('click', function () { zeigen(btn); });
    });
    var start = s.querySelector('[data-zeigen]');
    var wunsch = new URLSearchParams(location.search).get('clip');
    var gewuenscht = wunsch && s.querySelector('[data-zeigen="' + CSS.escape(wunsch) + '"]');
    if (gewuenscht) zeigen(gewuenscht);
    else if (start) start.setAttribute('aria-current', 'true');

    // In Fotos sichern: erst laden, dann mit einem zweiten Tipp teilen (iOS
    // verlangt fuer das Teilen-Menue eine frische Beruehrung).
    s.querySelectorAll('[data-sichern]').forEach(function (btn) {
      var datei = null;
      btn.addEventListener('click', function () {
        var name = btn.dataset.sichern;
        if (datei) {
          if (navigator.canShare && navigator.canShare({ files: [datei] })) {
            navigator.share({ files: [datei] }).then(function () { toast('Gespeichert'); }).catch(function (err) {
              if (err && err.name !== 'AbortError') toast('Teilen ging nicht', true);
            });
          } else {
            var a = document.createElement('a'); a.href = URL.createObjectURL(datei); a.download = name;
            document.body.appendChild(a); a.click(); a.remove();
          }
          return;
        }
        btn.disabled = true; btn.textContent = 'Lädt …';
        fetch(dateiUrl(name, btn.dataset.stand), { credentials: 'same-origin' })
          .then(function (r) { if (!r.ok) throw new Error(); return r.blob(); })
          .then(function (b) {
            var endung = name.split('.').pop().toLowerCase();
            datei = new File([b], name, { type: b.type || (endung === 'mov' ? 'video/quicktime' : 'video/mp4') });
            btn.disabled = false; btn.classList.add('bereit'); btn.textContent = 'In Fotos sichern';
          })
          .catch(function () { btn.disabled = false; btn.textContent = 'Nochmal versuchen'; });
      });
    });

    // Texte am Handy ändern: Ändern macht aus dem Text ein Feld, Speichern schreibt
    // v<nr>.<feld> über die vorhandene Speichern-Route in die meta.yaml.
    s.querySelectorAll('[data-bearbeiten]').forEach(function (btn) {
      var box = btn.closest('.t-text'), inhalt = box.querySelector('.t-text-inhalt');
      var feld = box.dataset.feld, nr = +box.dataset.nr, feldEl = null, abbrechen = null;
      function schliessen() {
        if (feldEl) { feldEl.remove(); feldEl = null; }
        if (abbrechen) { abbrechen.remove(); abbrechen = null; }
        inhalt.hidden = false; btn.textContent = 'Ändern'; btn.disabled = false;
      }
      btn.addEventListener('click', function () {
        if (!feldEl) {
          feldEl = document.createElement('textarea');
          feldEl.className = 't-text-feld';
          feldEl.value = inhalt.textContent;
          inhalt.hidden = true; inhalt.after(feldEl);
          var hoehe = function () { feldEl.style.height = 'auto'; feldEl.style.height = (feldEl.scrollHeight + 2) + 'px'; };
          feldEl.addEventListener('input', hoehe); hoehe(); feldEl.focus();
          abbrechen = document.createElement('button');
          abbrechen.type = 'button'; abbrechen.className = 't-kopieren'; abbrechen.textContent = 'Abbrechen';
          abbrechen.addEventListener('click', schliessen);
          btn.before(abbrechen);
          btn.textContent = 'Speichern';
          return;
        }
        var wert = feldEl.value, daten = {};
        daten['v' + nr + '.' + feld] = wert;
        btn.disabled = true; btn.textContent = 'Speichert …';
        fetch(T.speichern, { method: 'POST', credentials: 'same-origin', body: new URLSearchParams(daten) })
          .then(function (r) { return r.json(); })
          .then(function (j) {
            if (!j.ok) throw new Error(j.error || 'Speichern ging nicht');
            inhalt.textContent = wert;
            if (feld === 'oton') T.otons[nr - 1] = wert;
            if (feld === 'hook') T.hooks[nr - 1] = wert;
            schliessen(); toast('Gespeichert');
          })
          .catch(function (e) { btn.disabled = false; btn.textContent = 'Speichern'; toast(e.message || 'Speichern ging nicht', true); });
      });
    });

    s.querySelectorAll('[data-kopieren]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var text = btn.closest('.t-text').querySelector('.t-text-inhalt').textContent;
        navigator.clipboard.writeText(text).then(function () {
          btn.textContent = 'Kopiert';
          setTimeout(function () { btn.textContent = 'Kopieren'; }, 1500);
        }, function () { toast('Kopieren ging nicht', true); });
      });
    });
  });

  // ---- Aufnahme: Video, Doppelt (Front + Rueck), Foto ---------------------
  var rec = $('rec');
  if (!rec) return;
  var live = $('r-live'), pip = $('r-pip'), review = $('r-review'), reviewPip = $('r-review-pip'), foto = $('r-foto');
  var tp = $('r-tp'), tpText = $('r-tp-text'), tempo = $('r-tempo'), groesse = $('r-groesse');

  // Einstellungen, gemerkt im Browser.
  var E = { res: '1080', fps: '30', countdown: '3', tpPos: 'oben', spiegel: 'an', raster: 'aus', aufbereitung: 'aus', licht: 'aus', ring: 'aus' };
  try { Object.assign(E, JSON.parse(localStorage.getItem('pm-kamera') || '{}')); } catch (e) { /* egal */ }
  // Stand 2: Rauschunterdrueckung jetzt standardmaessig aus (klang wie Telefon).
  if (E.stand !== 2) { E.rauschen = 'aus'; E.stand = 2; }
  // Stand 3: statt einzelner Rauschunterdrueckung ein Schalter fuer die ganze
  // iPhone-Sprachaufbereitung (Echo, Lautstaerke, Rauschen), Standard aus.
  if (E.stand !== 3) { E.aufbereitung = 'aus'; delete E.rauschen; E.stand = 3; }
  function einstMerken() { merken('pm-kamera', JSON.stringify(E)); }
  var GROESSE = { '720': [1280, 720], '1080': [1920, 1080], '4k': [3840, 2160] };

  var z = { nr: 1, modus: 'video', front: true, stream: null, stream2: null, gleichzeitig: false,
            recs: [], teile: [[], []], blobs: [], fotoBlob: null, laeuft: false, schritt: 0,
            start: 0, tick: null, y: 0, pause: false, raf: null, wake: null, mime: '', ctx: null, tonKette: [] };

  tempo.value = lesen('pm-tp-tempo', '55');
  groesse.value = lesen('pm-tp-groesse', '30');
  function schriftSetzen() { tpText.style.fontSize = groesse.value + 'px'; }
  schriftSetzen();
  tempo.addEventListener('input', function () { merken('pm-tp-tempo', tempo.value); });
  groesse.addEventListener('input', function () { merken('pm-tp-groesse', groesse.value); schriftSetzen(); });

  function hinweis(text) {
    var el = $('r-hinweis');
    el.textContent = text || '';
    el.hidden = !text;
  }

  function mimeWaehlen() {
    if (!window.MediaRecorder) return '';
    var k = ['video/mp4;codecs=avc1.640028,mp4a.40.2', 'video/mp4;codecs=avc1.42E01E,mp4a.40.2', 'video/mp4;codecs=avc1,mp4a', 'video/mp4',
             'video/webm;codecs=vp9,opus', 'video/webm;codecs=vp8,opus', 'video/webm'];
    for (var i = 0; i < k.length; i++) { if (MediaRecorder.isTypeSupported(k[i])) return k[i]; }
    return '';
  }

  // ---- Teleprompter
  function teleprompterFuellen(nr) {
    var oton = (T.otons || [])[nr - 1] || '', hook = (T.hooks || [])[nr - 1] || '';
    tpText.textContent = '';
    if (!oton) { tpText.textContent = hook || 'Für diese Version gibt es noch keinen O-Ton.'; return; }
    if (hook && oton.indexOf(hook) === 0) {
      var h = document.createElement('span');
      h.className = 'r-tp-hook';
      h.textContent = hook;
      tpText.appendChild(h);
      tpText.appendChild(document.createTextNode(oton.slice(hook.length)));
    } else {
      tpText.textContent = oton;
    }
  }
  function tpZurueck() { z.y = 0; z.pause = false; tp.classList.remove('pause'); tpText.style.transform = 'translateY(0)'; }
  function tpLaufen() {
    var zuletzt = performance.now();
    cancelAnimationFrame(z.raf);
    function schritt(jetzt) {
      var dt = (jetzt - zuletzt) / 1000;
      zuletzt = jetzt;
      if (!z.pause) {
        var max = Math.max(0, tpText.offsetHeight - $('r-tp-fenster').clientHeight * 0.35);
        z.y = Math.min(max, z.y + Number(tempo.value) * dt);
        tpText.style.transform = 'translateY(' + (-z.y) + 'px)';
      }
      z.raf = requestAnimationFrame(schritt);
    }
    z.raf = requestAnimationFrame(schritt);
  }
  // Teleprompter-Feld frei verschieben und in der Groesse ziehen.
  // Gespeichert als Anteil der Bildschirmflaeche, damit es zu jedem Format passt.
  function tpBoxSetzen() {
    var b = E.tpBox;
    if (!b) {
      tp.classList.remove('frei');
      ['left', 'top', 'width', 'height', 'right'].forEach(function (k) { tp.style[k] = ''; });
      tp.classList.toggle('mitte', E.tpPos === 'mitte');
      return;
    }
    tp.classList.add('frei');
    tp.classList.remove('mitte');
    if (!rec.hidden) {
      // Nie hinter die Knoepfe: zu tief gespeicherte Felder rutschen nach oben.
      var unten = tpUntenGrenze();
      if (b.h > unten) b.h = unten;
      if (b.y + b.h > unten) b.y = Math.max(0, unten - b.h);
    }
    tp.style.left = (b.x * 100) + '%';
    tp.style.top = (b.y * 100) + '%';
    tp.style.width = (b.w * 100) + '%';
    tp.style.height = (b.h * 100) + '%';
    tp.style.right = 'auto';
  }
  function tpBoxAktuell() {
    var f = rec.getBoundingClientRect(), r = tp.getBoundingClientRect();
    return { x: (r.left - f.left) / f.width, y: (r.top - f.top) / f.height, w: r.width / f.width, h: r.height / f.height };
  }
  function ziehen(griff, art) {
    griff.addEventListener('pointerdown', function (e) {
      if (z.laeuft) return;
      e.preventDefault();
      e.stopPropagation();
      griff.setPointerCapture(e.pointerId);
      var f = rec.getBoundingClientRect();
      var start = { x: e.clientX, y: e.clientY, box: E.tpBox || tpBoxAktuell() };
      E.tpBox = Object.assign({}, start.box);
      tpBoxSetzen();
      function bewegen(ev) {
        var dx = (ev.clientX - start.x) / f.width, dy = (ev.clientY - start.y) / f.height, b = start.box;
        var unten = tpUntenGrenze();
        if (art === 'schieben') {
          E.tpBox.x = Math.min(Math.max(b.x + dx, 0), 1 - b.w);
          E.tpBox.y = Math.min(Math.max(b.y + dy, 0), unten - b.h);
        } else {
          E.tpBox.w = Math.min(Math.max(b.w + dx, 0.35), 1 - b.x);
          E.tpBox.h = Math.min(Math.max(b.h + dy, 0.1), unten - b.y);
        }
        tpBoxSetzen();
      }
      function los() {
        griff.removeEventListener('pointermove', bewegen);
        griff.removeEventListener('pointerup', los);
        griff.removeEventListener('pointercancel', los);
        einstMerken();
      }
      griff.addEventListener('pointermove', bewegen);
      griff.addEventListener('pointerup', los);
      griff.addEventListener('pointercancel', los);
    });
    griff.addEventListener('click', function (e) { e.stopPropagation(); });
  }
  // Unterkante, bis zu der das Feld reichen darf: oberhalb der Knoepfe unten.
  function tpUntenGrenze() {
    var f = rec.getBoundingClientRect(), u = $('r-unten').getBoundingClientRect();
    return Math.max(0.3, (u.top - f.top) / f.height - 0.01);
  }
  ziehen($('r-tp-ecke'), 'groesse');

  // Am ganzen Feld ziehen: vor der Aufnahme verschiebt es das
  // Feld, waehrend der Aufnahme scrollt es den Text von Hand. Kurz tippen
  // pausiert waehrend der Aufnahme.
  tp.addEventListener('pointerdown', function (e) {
    if (e.target.closest('#r-tp-ecke')) return;
    e.preventDefault();
    tp.setPointerCapture(e.pointerId);
    var f = rec.getBoundingClientRect();
    var st = { x: e.clientX, y: e.clientY, gezogen: false, box: null, textY: z.y, warPause: z.pause };
    if (z.laeuft) z.pause = true;
    function bewegen(ev) {
      var dxPx = ev.clientX - st.x, dyPx = ev.clientY - st.y;
      if (!st.gezogen && Math.abs(dxPx) + Math.abs(dyPx) < 8) return;
      st.gezogen = true;
      if (z.laeuft) {
        var max = Math.max(0, tpText.offsetHeight - $('r-tp-fenster').clientHeight * 0.35);
        z.y = Math.min(max, Math.max(0, st.textY - dyPx));
        tpText.style.transform = 'translateY(' + (-z.y) + 'px)';
        return;
      }
      if (!st.box) { st.box = E.tpBox || tpBoxAktuell(); E.tpBox = Object.assign({}, st.box); }
      var unten = tpUntenGrenze();
      E.tpBox.x = Math.min(Math.max(st.box.x + dxPx / f.width, 0), 1 - st.box.w);
      E.tpBox.y = Math.min(Math.max(st.box.y + dyPx / f.height, 0), unten - st.box.h);
      tpBoxSetzen();
    }
    function los() {
      tp.removeEventListener('pointermove', bewegen);
      tp.removeEventListener('pointerup', los);
      tp.removeEventListener('pointercancel', los);
      if (z.laeuft) {
        // Tippen schaltet Pause um, Ziehen laesst den Text danach weiterlaufen.
        z.pause = st.gezogen ? st.warPause : !st.warPause;
        tp.classList.toggle('pause', z.pause);
      } else if (st.gezogen) {
        einstMerken();
      }
    }
    tp.addEventListener('pointermove', bewegen);
    tp.addEventListener('pointerup', los);
    tp.addEventListener('pointercancel', los);
  });
  $('r-tp-reset').addEventListener('click', function () { E.tpBox = null; einstMerken(); tpBoxSetzen(); });


  $('r-tp-an').addEventListener('click', function () {
    var an = tp.classList.toggle('aus') === false;
    this.setAttribute('aria-pressed', an ? 'true' : 'false');
  });

  // ---- Kamera
  function videoVorgaben(front) {
    var g = GROESSE[E.res] || GROESSE['1080'];
    // Im Sensorformat anfragen, Safari dreht selbst ins Hochformat.
    return { facingMode: front ? 'user' : 'environment', width: { ideal: g[0] }, height: { ideal: g[1] }, frameRate: { ideal: Number(E.fps) } };
  }
  // Mikrofon: Sprachaufbereitung des iPhones (Echo, automatische Lautstaerke,
  // Rauschen) als ein Schalter. Aus klang duenn und nasal, an dumpf und gepresst
  //, deshalb Vergleichsaufnahme; der Dateiname merkt sich die Wahl.
  function tonVorgaben() {
    var an = E.aufbereitung === 'an';
    var t = { echoCancellation: an, autoGainControl: an, noiseSuppression: an,
              sampleRate: { ideal: 48000 }, channelCount: { ideal: 1 } };
    if (E.mikro) t.deviceId = { exact: E.mikro };
    return t;
  }
  function mikrofoneZeigen() {
    if (!navigator.mediaDevices.enumerateDevices) return;
    navigator.mediaDevices.enumerateDevices().then(function (geraete) {
      var mikros = geraete.filter(function (g) { return g.kind === 'audioinput'; });
      var zeile = $('r-mikro-zeile'), sel = $('r-mikro');
      zeile.hidden = mikros.length < 2;
      if (mikros.length < 2) return;
      var aktiv = z.stream && z.stream.getAudioTracks()[0] ? z.stream.getAudioTracks()[0].getSettings().deviceId : '';
      sel.innerHTML = '';
      mikros.forEach(function (m, i) {
        var o = document.createElement('option');
        o.value = m.deviceId;
        o.textContent = m.label || ('Mikrofon ' + (i + 1));
        if (m.deviceId === (E.mikro || aktiv)) o.selected = true;
        sel.appendChild(o);
      });
    }).catch(function () {});
  }
  // Safari legt die Mono-Stimme nur auf den linken Kanal, rechts bleibt stumm.
  // Deshalb laeuft der Ton beim Aufnehmen durch WebAudio: nur Kanal 1 nehmen
  // und auf links und rechts legen. Der Kontext entsteht beim Tippen auf den
  // Ausloeser, sonst laesst iOS ihn stumm.
  function tonKontext() {
    var AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    if (!z.ctx) {
      try { z.ctx = new AC({ sampleRate: 48000 }); }
      catch (e) { try { z.ctx = new AC(); } catch (e2) { z.ctx = null; return; } }
    }
    if (z.ctx.state === 'suspended') z.ctx.resume().catch(function () {});
  }
  function stereoStream(stream) {
    var spur = stream.getAudioTracks()[0];
    if (!spur || !z.ctx || z.ctx.state !== 'running' || !z.ctx.createMediaStreamDestination) return stream;
    try {
      var quelle = z.ctx.createMediaStreamSource(new MediaStream([spur]));
      // 1 Kanal, diskret: bei zwei Eingangskanaelen bleibt nur der linke.
      var mono = z.ctx.createGain();
      mono.channelCount = 1; mono.channelCountMode = 'explicit'; mono.channelInterpretation = 'discrete';
      // 2 Kanaele, Lautsprecher-Regel: Mono geht gleich laut auf links und rechts.
      var ziel = z.ctx.createMediaStreamDestination();
      ziel.channelCount = 2; ziel.channelCountMode = 'explicit'; ziel.channelInterpretation = 'speakers';
      quelle.connect(mono); mono.connect(ziel);
      z.tonKette = [quelle, mono];
      return new MediaStream(stream.getVideoTracks().concat(ziel.stream.getAudioTracks()));
    } catch (e) { tonKetteLoesen(); return stream; }
  }
  function tonKetteLoesen() {
    z.tonKette.forEach(function (k) { try { k.disconnect(); } catch (e) { /* egal */ } });
    z.tonKette = [];
  }
  // Nach der Aufnahme schliessen, sonst spielt iOS die Vorschau leise ueber die Hoermuschel.
  function tonKontextZu() {
    tonKetteLoesen();
    if (z.ctx) { z.ctx.close().catch(function () {}); z.ctx = null; }
  }
  function stoppeStream(st) { if (st) st.getTracks().forEach(function (t) { t.stop(); }); }
  function kameraStoppen() {
    stoppeStream(z.stream); stoppeStream(z.stream2);
    z.stream = null; z.stream2 = null; z.gleichzeitig = false;
    live.pause(); live.srcObject = null;
    pip.pause(); pip.srcObject = null; pip.hidden = true;
  }
  function spiegelSetzen() {
    var an = E.spiegel === 'an';
    live.classList.toggle('spiegel', an && z.front);
    review.classList.toggle('spiegel', an && z.front && z.modus !== 'doppelt');
    pip.classList.toggle('spiegel', an && !z.front);
    foto.classList.toggle('spiegel', false);
  }
  function faehigkeitenZeigen() {
    var track = z.stream && z.stream.getVideoTracks()[0];
    var caps = track && track.getCapabilities ? track.getCapabilities() : {};
    var zoom = $('r-zoom');
    if (caps.zoom && caps.zoom.max > caps.zoom.min) {
      zoom.min = caps.zoom.min; zoom.max = caps.zoom.max; zoom.step = caps.zoom.step || 0.1;
      var akt = track.getSettings ? track.getSettings().zoom : null;
      zoom.value = akt || caps.zoom.min;
      $('r-zoom-zeile').hidden = false;
    } else {
      $('r-zoom-zeile').hidden = true;
    }
    $('r-licht-zeile').hidden = !caps.torch;
    if (caps.torch && E.licht === 'an') track.applyConstraints({ advanced: [{ torch: true }] }).catch(function () {});
  }
  $('r-zoom').addEventListener('input', function () {
    var track = z.stream && z.stream.getVideoTracks()[0];
    if (track) track.applyConstraints({ advanced: [{ zoom: Number(this.value) }] }).catch(function () {});
  });

  function kameraStarten() {
    kameraStoppen();
    return navigator.mediaDevices.getUserMedia({
      video: videoVorgaben(z.front),
      audio: z.modus === 'foto' ? false : tonVorgaben()
    }).then(function (stream) {
      z.stream = stream;
      live.srcObject = stream;
      live.onloadedmetadata = function () { $('r-aufloesung').textContent = live.videoWidth + ' × ' + live.videoHeight; };
      spiegelSetzen();
      faehigkeitenZeigen();
      mikrofoneZeigen();
      return live.play().catch(function () {});
    }).then(function () {
      if (z.modus === 'doppelt' && !z.zweiterTake) return zweiteKameraVersuchen();
    });
  }

  // Doppelt: zweite Kamera gleichzeitig oeffnen. Geht das nicht (iPhone),
  // bleibt es bei einer Kamera und die Aufnahme laeuft nacheinander.
  function zweiteKameraVersuchen() {
    return navigator.mediaDevices.getUserMedia({ video: videoVorgaben(!z.front), audio: false })
      .then(function (st2) {
        return new Promise(function (fertig) { setTimeout(fertig, 900); }).then(function () {
          var t1 = z.stream && z.stream.getVideoTracks()[0], t2 = st2.getVideoTracks()[0];
          var beideLaufen = t1 && t2 && t1.readyState === 'live' && t2.readyState === 'live' && !t1.muted && !t2.muted;
          if (!beideLaufen) {
            stoppeStream(st2);
            z.gleichzeitig = false;
            hinweis('Dein Gerät schafft nur eine Kamera gleichzeitig. Erst nimmst du dich auf, danach automatisch die Rückkamera.');
            // Die erste Kamera kann durch den Versuch angehalten sein, also neu starten.
            if (!t1 || t1.readyState !== 'live' || t1.muted) {
              z.modus = 'doppelt-nach';
              return kameraStarten().then(function () { z.modus = 'doppelt'; });
            }
            return;
          }
          z.stream2 = st2;
          z.gleichzeitig = true;
          pip.srcObject = st2; pip.hidden = false; pip.play().catch(function () {});
          spiegelSetzen();
          hinweis('Beide Kameras laufen gleichzeitig.');
          setTimeout(function () { hinweis(''); }, 2500);
        });
      })
      .catch(function () {
        z.gleichzeitig = false;
        hinweis('Erst nimmst du dich auf, danach automatisch die Rückkamera.');
      });
  }

  // ---- Ansichten
  function ansicht(was) {
    var istLive = was === 'live';
    live.hidden = !istLive;
    pip.hidden = !istLive || !z.gleichzeitig;
    review.hidden = was !== 'review';
    reviewPip.hidden = was !== 'review' || z.blobs.length < 2;
    foto.hidden = was !== 'foto';
    $('r-live-knoepfe').hidden = !istLive;
    $('r-modi').hidden = !istLive;
    $('r-review-knoepfe').hidden = istLive;
    $('r-regler').hidden = !istLive || z.modus === 'foto';
    tp.hidden = !istLive || z.modus === 'foto';
    $('r-raster').hidden = !istLive || E.raster !== 'an';
    ringSetzen(istLive);
  }
  // Ringlicht: weisser, leuchtender Rand, der das Gesicht aufhellt.
  function ringSetzen(sichtbar) {
    var r = $('r-ringlicht');
    r.hidden = sichtbar === false || E.ring === 'aus';
    r.classList.toggle('stark', E.ring === 'stark');
  }
  function modusSetzen(m) {
    z.modus = m;
    document.querySelectorAll('[data-modus]').forEach(function (b) { b.setAttribute('aria-pressed', b.dataset.modus === m ? 'true' : 'false'); });
    $('r-ausloeser').classList.toggle('foto', m === 'foto');
    hinweis('');
    z.zweiterTake = false;
    if (m === 'doppelt') z.front = true;
    ansicht('live');
    kameraStarten().catch(kameraFehler);
  }
  document.querySelectorAll('[data-modus]').forEach(function (b) {
    b.addEventListener('click', function () { if (!z.laeuft) modusSetzen(b.dataset.modus); });
  });

  function kameraFehler(e) {
    schliessen();
    toast(e && e.name === 'NotAllowedError' ? 'Kamera oder Mikrofon nicht erlaubt. In den Browser-Einstellungen freigeben.' : 'Kamera ließ sich nicht starten.', true);
  }

  function oeffnen(nr) {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.isSecureContext) {
      toast('Aufnehmen im Browser braucht eine sichere Verbindung. Öffne postmakr über die HTTPS-Adresse oder nimm solange die Kamera-App.', true);
      return;
    }
    z.mime = mimeWaehlen();
    if (!z.mime) { toast('Dieser Browser kann nicht aufnehmen. Nimm die Kamera-App.', true); return; }
    z.nr = Number(nr);
    z.front = true;
    z.blobs = []; z.fotoBlob = null;
    teleprompterFuellen(z.nr);
    tpZurueck();
    rec.hidden = false;
    tpBoxSetzen();
    document.body.classList.add('gesperrt');
    modusSetzen(z.modus === 'doppelt-nach' ? 'doppelt' : (z.modus || 'video'));
  }

  function reviewLeeren() {
    [review, reviewPip].forEach(function (v) {
      v.pause();
      if (v.src) URL.revokeObjectURL(v.src);
      v.removeAttribute('src');
      v.load();
    });
    if (foto.src) URL.revokeObjectURL(foto.src);
    foto.removeAttribute('src');
  }

  function schliessen() {
    if (z.laeuft) stoppen(true);
    kameraStoppen();
    cancelAnimationFrame(z.raf);
    reviewLeeren();
    tonKontextZu();
    z.blobs = []; z.fotoBlob = null;
    hinweis('');
    rec.hidden = true;
    rec.classList.remove('laeuft');
    $('r-panel').hidden = true;
    document.body.classList.remove('gesperrt');
  }

  function countdown(fertig) {
    var n = Number(E.countdown) || 0;
    if (!n) { fertig(); return; }
    var el = $('r-countdown');
    el.hidden = false;
    el.textContent = n;
    var t = setInterval(function () {
      n -= 1;
      if (n <= 0) { clearInterval(t); el.hidden = true; fertig(); return; }
      el.textContent = n;
    }, 1000);
  }

  function recorder(stream, index) {
    var r;
    try { r = new MediaRecorder(stream, { mimeType: z.mime, videoBitsPerSecond: E.res === '4k' ? 20000000 : 8000000, audioBitsPerSecond: 192000 }); }
    catch (e) { r = new MediaRecorder(stream); }
    z.teile[index] = [];
    r.ondataavailable = function (e) { if (e.data && e.data.size) z.teile[index].push(e.data); };
    return r;
  }

  function starten() {
    if (!z.stream) return;
    tonKontext();
    countdown(function () {
      tonKetteLoesen();
      z.recs = [recorder(stereoStream(z.stream), 0)];
      if (z.gleichzeitig && z.stream2) {
        // Ton nur einmal: die Rueckkamera-Spur bekommt das Mikro mit dazu.
        z.recs.push(recorder(z.stream2, 1));
      }
      var offen = z.recs.length;
      z.recs.forEach(function (r) { r.onstop = function () { offen -= 1; if (!offen) fertigAufgenommen(); }; r.start(1000); });
      z.laeuft = true;
      rec.classList.add('laeuft');
      hinweis('');
      tpZurueck();
      tpLaufen();
      z.start = Date.now();
      var zeit = $('r-zeit');
      zeit.hidden = false;
      zeit.textContent = '0:00';
      clearInterval(z.tick);
      z.tick = setInterval(function () {
        var s = Math.floor((Date.now() - z.start) / 1000);
        zeit.textContent = Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
      }, 250);
      if (navigator.wakeLock) navigator.wakeLock.request('screen').then(function (w) { z.wake = w; }).catch(function () {});
    });
  }

  function stoppen(verwerfen) {
    z.laeuft = false;
    rec.classList.remove('laeuft');
    clearInterval(z.tick);
    cancelAnimationFrame(z.raf);
    $('r-zeit').hidden = true;
    if (z.wake) { z.wake.release().catch(function () {}); z.wake = null; }
    z.recs.forEach(function (r) {
      if (verwerfen) r.onstop = null;
      if (r.state !== 'inactive') r.stop();
    });
  }

  function blobAus(index) {
    var typ = (z.recs[index] && z.recs[index].mimeType) || z.mime || 'video/webm';
    return new Blob(z.teile[index], { type: typ.split(';')[0] });
  }

  function fertigAufgenommen() {
    z.blobs.push(blobAus(0));
    if (z.recs.length > 1) z.blobs.push(blobAus(1));
    // Doppelt nacheinander: nach dem ersten Take automatisch die Rueckkamera.
    if (z.modus === 'doppelt' && !z.gleichzeitig && z.blobs.length === 1) {
      z.front = false;
      z.zweiterTake = true;
      hinweis('Jetzt die Rückkamera: Zeig, wovon du gerade gesprochen hast.');
      kameraStarten().then(function () { ansicht('live'); setTimeout(starten, 600); }).catch(kameraFehler);
      return;
    }
    kameraStoppen();
    tonKontextZu();
    z.zweiterTake = false;
    review.src = URL.createObjectURL(z.blobs[0]);
    if (z.blobs[1]) { reviewPip.src = URL.createObjectURL(z.blobs[1]); }
    review.classList.toggle('spiegel', E.spiegel === 'an' && z.modus === 'video' && z.front);
    ansicht('review');
    hinweis('');
    review.play().catch(function () {});
    if (z.blobs[1]) reviewPip.play().catch(function () {});
  }

  function fotoMachen() {
    var w = live.videoWidth, h = live.videoHeight;
    if (!w || !h) return;
    var c = document.createElement('canvas');
    c.width = w; c.height = h;
    c.getContext('2d').drawImage(live, 0, 0, w, h);
    c.toBlob(function (blob) {
      z.fotoBlob = blob;
      kameraStoppen();
      foto.src = URL.createObjectURL(blob);
      ansicht('foto');
    }, 'image/jpeg', 0.92);
  }

  // ---- Knoepfe
  $('r-ausloeser').addEventListener('click', function () {
    if (z.modus === 'foto') { fotoMachen(); return; }
    if (z.laeuft) stoppen(false); else { z.blobs = []; starten(); }
  });
  $('r-kamera').addEventListener('click', function () {
    if (z.laeuft) return;
    z.front = !z.front;
    kameraStarten().catch(function () { toast('Kamera ließ sich nicht wechseln.', true); });
  });
  $('r-zu').addEventListener('click', schliessen);
  $('r-nochmal').addEventListener('click', function () {
    reviewLeeren();
    z.blobs = []; z.fotoBlob = null;
    z.zweiterTake = false;
    if (z.modus === 'doppelt') z.front = true;
    tpZurueck();
    ansicht('live');
    kameraStarten().catch(kameraFehler);
  });
  $('r-verwenden').addEventListener('click', function () {
    var btn = this;
    var jetzt = new Date();
    var stempel = [jetzt.getHours(), jetzt.getMinutes(), jetzt.getSeconds()].map(function (x) { return String(x).padStart(2, '0'); }).join('');
    var dateien = [];
    if (z.fotoBlob) {
      dateien.push({ datei: z.fotoBlob, name: 'foto_v' + z.nr + '_' + stempel + '.jpg' });
    } else {
      z.blobs.forEach(function (b, i) {
        var endung = b.type.indexOf('mp4') !== -1 ? 'mp4' : 'webm';
        var teil = z.modus === 'doppelt' ? (i === 0 ? '_front' : '_rueck') : '';
        var ton = '_ton-' + (E.aufbereitung === 'an' ? 'an' : 'aus');
        dateien.push({ datei: b, name: 'aufnahme_v' + z.nr + '_' + stempel + teil + ton + '.' + endung });
      });
    }
    if (!dateien.length) return;
    btn.disabled = true;
    hochladen(z.nr, dateien, $('r-balken'), function (ok) {
      btn.disabled = false;
      if (ok) { var nr = z.nr; schliessen(); neuLaden(nr); }
    });
  });

  // ---- Einstellungen
  function panelAnzeigen() {
    document.querySelectorAll('#r-panel [data-einst]').forEach(function (gruppe) {
      gruppe.querySelectorAll('button').forEach(function (b) {
        b.setAttribute('aria-pressed', String(E[gruppe.dataset.einst]) === b.dataset.wert ? 'true' : 'false');
      });
    });
  }
  $('r-einst').addEventListener('click', function () {
    if (z.laeuft) return;
    panelAnzeigen();
    $('r-panel').hidden = false;
  });
  $('r-panel-zu').addEventListener('click', function () { $('r-panel').hidden = true; });
  $('r-mikro').addEventListener('change', function () {
    E.mikro = this.value;
    einstMerken();
    kameraStarten().catch(function () { toast('Dieses Mikrofon ließ sich nicht öffnen.', true); });
  });
  document.querySelectorAll('#r-panel [data-einst]').forEach(function (gruppe) {
    gruppe.addEventListener('click', function (e) {
      var b = e.target.closest('button');
      if (!b) return;
      var key = gruppe.dataset.einst;
      E[key] = b.dataset.wert;
      einstMerken();
      panelAnzeigen();
      if (key === 'res' || key === 'fps' || key === 'aufbereitung') {
        kameraStarten().catch(function () { toast('Diese Einstellung kann die Kamera nicht.', true); });
      } else if (key === 'spiegel') {
        spiegelSetzen();
      } else if (key === 'ring') {
        ringSetzen(true);
      } else if (key === 'raster') {
        $('r-raster').hidden = E.raster !== 'an';
      } else if (key === 'tpPos') {
        E.tpBox = null;
        einstMerken();
        tpBoxSetzen();
      } else if (key === 'licht') {
        var track = z.stream && z.stream.getVideoTracks()[0];
        if (track) track.applyConstraints({ advanced: [{ torch: E.licht === 'an' }] }).catch(function () {});
      }
    });
  });

  document.querySelectorAll('[data-aufnehmen]').forEach(function (btn) {
    btn.addEventListener('click', function () { oeffnen(btn.dataset.aufnehmen); });
  });
  // App in den Hintergrund: Aufnahme beenden (landet in der Vorschau),
  // Wiedergabe anhalten. Seite verlassen: alles schliessen.
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) return;
    if (z.laeuft) stoppen(false);
    review.pause(); reviewPip.pause();
  });
  window.addEventListener('pagehide', function () { if (!rec.hidden) schliessen(); });
})();
