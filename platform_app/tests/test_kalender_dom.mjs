/* ============================================================================
   Node-DOM-Test des Kalender-Skripts (templates/_kalender.html)
   ----------------------------------------------------------------------------
   Geprueft wird, was auf dem Server niemand sehen kann: die Kanal-Klassen der
   Karten („alle weiss, ich will bunt nach Plattform") und die
   Filter-Pillen in der Kopfzeile samt Merken der Wahl.

   Ohne Fremdpakete: Markup und Skript kommen aus dem echten Template, der
   Rest ist ein winziger DOM-Nachbau, der genau die Aufrufe kennt, die das
   Skript macht. So bleibt der Test mit `node --test` startklar.

   Lauf:  node --test platform_app/tests/test_kalender_dom.mjs
   ============================================================================ */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const HIER = dirname(fileURLToPath(import.meta.url));
const TEMPLATE = join(HIER, '..', 'templates', '_kalender.html');

// ---------------------------------------------------------------------------
// Winziger DOM-Nachbau
// ---------------------------------------------------------------------------
const kebab = (s) => s.replace(/[A-Z]/g, (c) => '-' + c.toLowerCase());
const LEER_TAGS = new Set(['br', 'hr', 'img', 'input', 'meta', 'link', 'path',
  'circle', 'rect', 'line', 'polyline', 'polygon', 'ellipse', 'use', 'stop']);

class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.attrs = Object.create(null);
    this.listeners = Object.create(null);
    this.style = {};
    this._text = '';
    const self = this;
    this.dataset = new Proxy(Object.create(null), {
      get: (_t, k) => self.attrs['data-' + kebab(String(k))],
      set: (_t, k, v) => { self.attrs['data-' + kebab(String(k))] = String(v); return true; },
      has: (_t, k) => ('data-' + kebab(String(k))) in self.attrs,
    });
    this.classList = {
      add: (c) => { if (!self._klassen().includes(c)) self.className = (self.className + ' ' + c).trim(); },
      remove: (c) => { self.className = self._klassen().filter((x) => x !== c).join(' '); },
      contains: (c) => self._klassen().includes(c),
      toggle: (c, an) => { const soll = an === undefined ? !self.classList.contains(c) : !!an; soll ? self.classList.add(c) : self.classList.remove(c); },
    };
  }
  _klassen() { return (this.attrs.class || '').split(/\s+/).filter(Boolean); }

  get className() { return this.attrs.class || ''; }
  set className(v) { this.attrs.class = String(v); }
  get id() { return this.attrs.id || ''; }
  set id(v) { this.attrs.id = String(v); }
  get hidden() { return this.attrs.hidden === 'true' || this.attrs.hidden === ''; }
  set hidden(v) { if (v) this.attrs.hidden = 'true'; else delete this.attrs.hidden; }
  get type() { return this.attrs.type || ''; }
  set type(v) { this.attrs.type = String(v); }
  get title() { return this.attrs.title || ''; }
  set title(v) { this.attrs.title = String(v); }
  get src() { return this.attrs.src || ''; }
  set src(v) { this.attrs.src = String(v); }
  get href() { return this.attrs.href || ''; }
  set href(v) { this.attrs.href = String(v); }
  get alt() { return this.attrs.alt || ''; }
  set alt(v) { this.attrs.alt = String(v); }
  get loading() { return this.attrs.loading || ''; }
  set loading(v) { this.attrs.loading = String(v); }

  setAttribute(n, v) { this.attrs[n] = String(v); }
  getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; }
  removeAttribute(n) { delete this.attrs[n]; }

  get textContent() {
    if (this.children.length) return this.children.map((c) => c.textContent).join('');
    return this._text;
  }
  set textContent(v) { this._text = String(v); this.children = []; }

  get firstElementChild() { return this.children[0] || null; }

  set innerHTML(markup) {
    this.children = [];
    this._text = '';
    const roh = String(markup);
    if (roh.trim()) for (const kind of parseFragment(roh)) this.appendChild(kind);
  }

  appendChild(kind) { kind.parentNode = this; this.children.push(kind); return kind; }
  removeChild(kind) { this.children = this.children.filter((c) => c !== kind); return kind; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); this.parentNode = null; }
  contains(el) { if (el === this) return true; return this.children.some((c) => c.contains(el)); }

  addEventListener(typ, fn) { (this.listeners[typ] = this.listeners[typ] || []).push(fn); }
  dispatch(typ, ereignis) { (this.listeners[typ] || []).forEach((fn) => fn.call(this, ereignis)); }

  // Klick, der wie im Browser bis zum Dokument hochblubbert.
  klick() {
    const ereignis = { type: 'click', target: this, preventDefault() {}, stopPropagation() {} };
    let knoten = this;
    while (knoten) { knoten.dispatch('click', ereignis); knoten = knoten.parentNode; }
    dokument.dispatch('click', ereignis);
  }

  matches(sel) {
    return sel.split(',').map((s) => s.trim()).filter(Boolean).some((teil) => {
      const stuecke = teil.match(/(\[[^\]]+\]|\.[\w-]+|#[\w-]+|^[\w-]+)/g) || [];
      return stuecke.every((st) => {
        if (st.startsWith('[')) {
          const m = st.slice(1, -1).match(/^([\w-]+)(?:=["']?([^"'\]]*)["']?)?$/);
          if (!m) return false;
          if (!(m[1] in this.attrs)) return false;
          return m[2] === undefined || this.attrs[m[1]] === m[2];
        }
        if (st.startsWith('.')) return this._klassen().includes(st.slice(1));
        if (st.startsWith('#')) return this.id === st.slice(1);
        return this.tagName === st.toUpperCase();
      });
    });
  }

  querySelectorAll(sel) {
    const treffer = [];
    const lauf = (el) => el.children.forEach((k) => { if (k.matches(sel)) treffer.push(k); lauf(k); });
    lauf(this);
    return treffer;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  closest(sel) {
    let k = this;
    while (k) { if (k.matches && k.matches(sel)) return k; k = k.parentNode; }
    return null;
  }
  focus() { dokument.aktiv = this; }
  getBoundingClientRect() {
    const h = this._hoehe === undefined ? 30 : this._hoehe;
    return { left: 10, top: 10, right: 90, bottom: 10 + h, width: 80, height: h };
  }
}

/** Sehr kleiner HTML-Leser: reicht fuer das Kalender-Markup und die SVGs. */
function parseFragment(html) {
  const wurzeln = [];
  const stapel = [];
  const muster = /<\/?([\w-]+)((?:\s+[\w:-]+(?:=(?:"[^"]*"|'[^']*'))?)*)\s*(\/?)>|([^<]+)/g;
  let m;
  while ((m = muster.exec(html)) !== null) {
    if (m[4] !== undefined) {
      const text = m[4];
      if (text.trim() && stapel.length) stapel[stapel.length - 1]._text += text.trim();
      continue;
    }
    const ganz = m[0];
    const tag = m[1];
    if (ganz.startsWith('</')) { stapel.pop(); continue; }
    const el = new El(tag);
    for (const a of m[2].matchAll(/([\w:-]+)(?:="([^"]*)"|='([^']*)')?/g)) {
      el.attrs[a[1]] = a[2] !== undefined ? a[2] : (a[3] !== undefined ? a[3] : '');
    }
    if (stapel.length) stapel[stapel.length - 1].appendChild(el); else wurzeln.push(el);
    if (!m[3] && !LEER_TAGS.has(tag.toLowerCase())) stapel.push(el);
  }
  return wurzeln;
}

// ---------------------------------------------------------------------------
// Seite aufbauen: Markup + Skript kommen aus dem echten Template
// ---------------------------------------------------------------------------
const quelle = readFileSync(TEMPLATE, 'utf8');
// Kalender plus Planen-Dialog. Jinja raus: Kommentare und
// Steuer-Tags weg, freistehende Ausdruecke im Tag (checked) weg, der Rest
// wird zur API-Adresse.
const markup = quelle
  .slice(quelle.indexOf('<section class="kalender"'), quelle.indexOf('</dialog>') + 9)
  .replace(/\{#[\s\S]*?#\}/g, '')
  .replace(/\{%[\s\S]*?%\}/g, '')
  .replace(/\s\{\{[^}]*\}\}(?=\s*>)/g, '')
  .replace(/\{\{[^}]*\}\}/g, '/w/demo/api/kalender');
// Im Skript stehen Jinja-Ausdruecke fuer Symbole, Namen und Reihenfolge der
// Kanaele des Workspace. Der Test setzt dafuer vier Kanaele ein.
const KANAELE = { instagram: 'Instagram', tiktok: 'TikTok', facebook: 'Facebook', linkedin: 'LinkedIn' };
const KANAL_SVG = Object.fromEntries(Object.keys(KANAELE).map((k) => [k, '<svg class="kalender-kanal"></svg>']));
const skript = quelle.slice(quelle.indexOf('<script>') + 8, quelle.lastIndexOf('</script>'))
  .replace(/var KANAL_SVG = \{[\s\S]*?\n  \};/, () => 'var KANAL_SVG = ' + JSON.stringify(KANAL_SVG) + ';')
  .replace('{{ kanal_namen | tojson }}', () => JSON.stringify(KANAELE))
  .replace(/\{\{ kanal_verzeichnis\(\)[^}]*\}\}/, () => JSON.stringify(Object.keys(KANAELE)));

let dokument;
let fensterEintraege = [];

function seiteBauen() {
  const body = new El('body');
  const html = new El('html');
  html.appendChild(body);
  for (const el of parseFragment(markup)) body.appendChild(el);

  dokument = {
    body,
    documentElement: html,
    listeners: Object.create(null),
    createElement: (t) => new El(t),
    getElementById: (id) => body.querySelectorAll('[id="' + id + '"]')[0] || null,
    addEventListener: (t, fn) => { (dokument.listeners[t] = dokument.listeners[t] || []).push(fn); },
    dispatch: (t, e) => (dokument.listeners[t] || []).forEach((fn) => fn(e)),
  };

  const speicher = new Map();
  const fenster = {
    addEventListener() {},
    scrollX: 0,
    scrollY: 0,
    localStorage: {
      getItem: (k) => (speicher.has(k) ? speicher.get(k) : null),
      setItem: (k, v) => speicher.set(k, String(v)),
    },
    fetch: () => Promise.resolve({
      ok: true,
      json: () => Promise.resolve({ ok: true, eintraege: fensterEintraege }),
    }),
    confirm: () => true,
  };
  fenster.window = fenster;
  fenster.document = dokument;
  fenster.localStorage = fenster.localStorage;
  fenster.console = console;
  fenster.setTimeout = setTimeout;
  fenster.Date = Date;
  fenster.encodeURIComponent = encodeURIComponent;
  fenster.URLSearchParams = URLSearchParams;

  vm.runInNewContext(skript, fenster);
  return fenster;
}

const warten = () => new Promise((r) => setTimeout(r, 0));

function eintrag(felder) {
  return Object.assign({
    id: 'aaaaaaaa', kind: 'drafts', slug: 'x', titel: 'Titel',
    datum: '2026-08-20', uhrzeit: '17:00', kanal: 'instagram',
    status: 'geplant', archiv: false, serie: false,
    vorschau_url: null, detail_url: '/w/demo/beitrag/drafts/x',
  }, felder);
}

// Der Kalender zeichnet den laufenden Monat, die Testdaten muessen also im
// Heute-Monat liegen.
function imMonat(tag, felder) {
  const d = new Date();
  const datum = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') +
    '-' + String(tag).padStart(2, '0');
  return eintrag(Object.assign({ datum }, felder));
}

async function kalenderStarten(eintraege, rasterHoehe) {
  fensterEintraege = eintraege;
  const fenster = seiteBauen();
  if (rasterHoehe !== undefined) dokument.getElementById('kalender-raster')._hoehe = rasterHoehe;
  fenster.kalenderAnzeigen(true);
  await warten();
  await warten();
  return fenster;
}

// Sechs Monatszeilen mal reichlich Platz: drei Karten passen in eine Zelle.
const VIEL_PLATZ = 6 * 130;

// ---------------------------------------------------------------------------
// Kanal-Farben auf den Karten
// ---------------------------------------------------------------------------
test('Karten tragen die Kanal-Klasse ihrer Plattform', async () => {
  await kalenderStarten([
    imMonat(3, { id: 'a1', kanal: 'instagram' }),
    imMonat(4, { id: 'a2', kanal: 'tiktok' }),
    imMonat(5, { id: 'a3', kanal: 'facebook' }),
    imMonat(6, { id: 'a4', kanal: 'linkedin' }),
  ]);
  const raster = dokument.getElementById('kalender-raster');
  for (const kanal of ['instagram', 'tiktok', 'facebook', 'linkedin']) {
    const karten = raster.querySelectorAll('.kal-kanal-' + kanal);
    assert.equal(karten.length, 1, 'genau eine Karte fuer ' + kanal);
    assert.ok(karten[0].matches('.kal-kanal'), kanal + ' traegt auch die Sammelklasse');
  }
});

test('Ohne bekannten Kanal bleibt die Karte farblos', async () => {
  await kalenderStarten([imMonat(7, { id: 'b1', kanal: '' })]);
  const raster = dokument.getElementById('kalender-raster');
  assert.equal(raster.querySelectorAll('.kal-kanal').length, 0);
  assert.equal(raster.querySelectorAll('.kalender-eintrag').length, 1);
});

test('Archiv-Eintrag mit Kanal wird ebenso gefaerbt', async () => {
  await kalenderStarten([
    imMonat(8, { id: 'c1', kanal: 'instagram', archiv: true, status: 'gepostet', uhrzeit: '' }),
  ]);
  const karte = dokument.getElementById('kalender-raster').querySelector('.kalender-eintrag');
  assert.ok(karte.matches('.archiv'));
  assert.ok(karte.matches('.kal-kanal-instagram'));
});

test('Facebook und LinkedIn haben ein eigenes Symbol', async () => {
  await kalenderStarten([
    imMonat(9, { id: 'd1', kanal: 'facebook' }),
    imMonat(10, { id: 'd2', kanal: 'linkedin' }),
  ]);
  const raster = dokument.getElementById('kalender-raster');
  assert.equal(raster.querySelectorAll('.kanal-facebook').length, 1);
  assert.equal(raster.querySelectorAll('.kanal-linkedin').length, 1);
  assert.equal(raster.querySelector('.kanal-facebook').tagName, 'SVG');
});

// ---------------------------------------------------------------------------
// Wie viele Karten in eine Zelle passen, haengt an der Fensterhoehe
// ---------------------------------------------------------------------------
test('Hohes Fenster zeigt drei Karten am selben Tag', async () => {
  await kalenderStarten([
    imMonat(12, { id: 'h1', kanal: 'instagram' }),
    imMonat(12, { id: 'h2', kanal: 'tiktok' }),
    imMonat(12, { id: 'h3', kanal: 'facebook' }),
  ], VIEL_PLATZ);
  const raster = dokument.getElementById('kalender-raster');
  assert.equal(raster.querySelectorAll('.kalender-eintrag').length, 3);
  assert.equal(raster.querySelectorAll('.kalender-stapel').length, 0);
});

test('Vier am selben Tag werden zur Reihe aus Mini-Vorschauen', async () => {
  await kalenderStarten([
    imMonat(12, { id: 'i1', kanal: 'instagram' }),
    imMonat(12, { id: 'i2', kanal: 'tiktok' }),
    imMonat(12, { id: 'i3', kanal: 'facebook' }),
    imMonat(12, { id: 'i4', kanal: 'linkedin' }),
  ], VIEL_PLATZ);
  const raster = dokument.getElementById('kalender-raster');
  assert.equal(raster.querySelectorAll('.kalender-eintrag').length, 0);
  assert.equal(raster.querySelectorAll('.kalender-stapel').length, 1);
  assert.equal(raster.querySelectorAll('.kalender-stapel-bild').length, 4);
});

test('Flaches Fenster schneidet nichts an, sondern stapelt frueher', async () => {
  await kalenderStarten([
    imMonat(12, { id: 'j1', kanal: 'instagram' }),
    imMonat(12, { id: 'j2', kanal: 'tiktok' }),
  ], 6 * 60);
  const raster = dokument.getElementById('kalender-raster');
  assert.equal(raster.querySelectorAll('.kalender-eintrag').length, 0);
  assert.equal(raster.querySelectorAll('.kalender-stapel-bild').length, 2);
});

// ---------------------------------------------------------------------------
// Filter-Pillen
// ---------------------------------------------------------------------------
test('Filter zeigt immer alle bekannten Kanaele, Alle steht vorn', async () => {
  await kalenderStarten([
    imMonat(3, { id: 'e1', kanal: 'instagram' }),
    imMonat(4, { id: 'e2', kanal: 'linkedin' }),
  ]);
  const leiste = dokument.getElementById('kalender-filter');
  assert.equal(leiste.hidden, false);
  const pillen = leiste.querySelectorAll('[data-kal-filter]');
  // Stabile Leiste: alle vier Kanaele stehen immer da,
  // unabhaengig davon, was der sichtbare Monat hergibt.
  assert.deepEqual(pillen.map((p) => p.dataset.kalFilter),
    ['alle', 'instagram', 'tiktok', 'facebook', 'linkedin']);
  assert.equal(pillen[0].getAttribute('aria-pressed'), 'true');
  assert.equal(pillen[1].textContent, 'Instagram');
  // Kanaele ohne Eintrag im Monat sind da, aber gedimmt.
  assert.ok(leiste.querySelector('[data-kal-filter="tiktok"]').className.includes('leer'));
  assert.ok(!leiste.querySelector('[data-kal-filter="instagram"]').className.includes('leer'));
});

test('Ohne Eintraege bleibt die Filterleiste trotzdem stehen', async () => {
  await kalenderStarten([]);
  const leiste = dokument.getElementById('kalender-filter');
  assert.equal(leiste.hidden, false);
  const pillen = leiste.querySelectorAll('[data-kal-filter]');
  assert.deepEqual(pillen.map((p) => p.dataset.kalFilter),
    ['alle', 'instagram', 'tiktok', 'facebook', 'linkedin']);
});

test('Klick auf eine Pille filtert die Karten und merkt sich die Wahl', async () => {
  const fenster = await kalenderStarten([
    imMonat(3, { id: 'f1', kanal: 'instagram' }),
    imMonat(4, { id: 'f2', kanal: 'tiktok' }),
    imMonat(5, { id: 'f3', kanal: 'tiktok' }),
  ]);
  const leiste = dokument.getElementById('kalender-filter');
  const raster = dokument.getElementById('kalender-raster');
  assert.equal(raster.querySelectorAll('.kalender-eintrag').length, 3);

  leiste.querySelector('[data-kal-filter="tiktok"]').klick();
  assert.equal(raster.querySelectorAll('.kalender-eintrag').length, 2);
  assert.equal(raster.querySelectorAll('.kal-kanal-instagram').length, 0);
  assert.equal(fenster.localStorage.getItem('postmakr-kalender-kanal'), 'tiktok');
  assert.equal(leiste.querySelector('[data-kal-filter="tiktok"]').getAttribute('aria-pressed'), 'true');
  assert.equal(leiste.querySelector('[data-kal-filter="alle"]').getAttribute('aria-pressed'), 'false');

  leiste.querySelector('[data-kal-filter="alle"]').klick();
  assert.equal(raster.querySelectorAll('.kalender-eintrag').length, 3);
  assert.equal(fenster.localStorage.getItem('postmakr-kalender-kanal'), 'alle');
});

test('Leerer Filter erklaert sich und bleibt anklickbar', async () => {
  await kalenderStarten([
    imMonat(3, { id: 'g1', kanal: 'instagram' }),
    imMonat(4, { id: 'g2', kanal: 'facebook' }),
  ]);
  const leiste = dokument.getElementById('kalender-filter');
  const meldung = dokument.getElementById('kalender-meldung');
  const raster = dokument.getElementById('kalender-raster');
  leiste.querySelector('[data-kal-filter="facebook"]').klick();
  assert.equal(raster.querySelectorAll('.kalender-eintrag').length, 1);
  assert.equal(meldung.hidden, true);

  // Nachgeladener Zeitraum ohne Facebook: die Pille bleibt stehen, damit man
  // wieder herausfindet, und die Meldung sagt, warum nichts zu sehen ist.
  fensterEintraege = [imMonat(5, { id: 'g3', kanal: 'instagram' })];
  dokument.getElementById('kalender').querySelector('[data-kal-heute]').klick();
  await warten();
  await warten();
  assert.equal(raster.querySelectorAll('.kalender-eintrag').length, 0);
  assert.equal(meldung.hidden, false);
  assert.match(meldung.textContent, /Facebook/);
  assert.ok(leiste.querySelector('[data-kal-filter="facebook"]'), 'Pille bleibt erreichbar');

  leiste.querySelector('[data-kal-filter="alle"]').klick();
  assert.equal(raster.querySelectorAll('.kalender-eintrag').length, 1);
  assert.equal(meldung.hidden, true);
});

// ---------------------------------------------------------------------------
// Selbst planen: Plus je Tag, Dialog, Ziehen
// ---------------------------------------------------------------------------
function heuteIso() {
  const d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}

test('Plus erscheint nur an Tagen ab heute', async () => {
  await kalenderStarten([]);
  const raster = dokument.getElementById('kalender-raster');
  const heute = heuteIso();
  for (const zelle of raster.querySelectorAll('.kalender-tag')) {
    const plus = zelle.querySelector('.kalender-tag-plus');
    if (zelle.dataset.kalTag >= heute) assert.ok(plus, 'Plus fehlt am ' + zelle.dataset.kalTag);
    else assert.equal(plus, null, 'Plus in der Vergangenheit am ' + zelle.dataset.kalTag);
  }
  assert.ok(raster.querySelector('[data-kal-plan-tag="' + heute + '"]'));
});

test('Klick aufs Plus oeffnet den Dialog mit diesem Tag', async () => {
  await kalenderStarten([]);
  const heute = heuteIso();
  dokument.getElementById('kalender-raster').querySelector('[data-kal-plan-tag="' + heute + '"]').klick();
  const dialog = dokument.getElementById('kp-dialog');
  assert.notEqual(dialog.getAttribute('open'), null);
  assert.equal(dokument.getElementById('kp-datum').value, heute);
  assert.equal(dokument.getElementById('kp-datum').min, heute);
  assert.equal(dokument.aktiv, dokument.getElementById('kp-suche'));
});

test('Planen-Knopf oeffnet den Dialog, Abbrechen schliesst ihn', async () => {
  await kalenderStarten([]);
  dokument.getElementById('kalender').querySelector('[data-kal-planen]').klick();
  const dialog = dokument.getElementById('kp-dialog');
  assert.notEqual(dialog.getAttribute('open'), null);
  dokument.getElementById('kp-abbrechen').klick();
  assert.equal(dialog.getAttribute('open'), null);
});

test('Nur geplante Eintraege lassen sich ziehen', async () => {
  await kalenderStarten([
    imMonat(3, { id: 'k1', kanal: 'instagram' }),
    imMonat(4, { id: 'k2', kanal: 'instagram', archiv: true, status: 'gepostet', uhrzeit: '' }),
    imMonat(5, { id: 'k3', kanal: 'instagram', status: 'gepostet' }),
    imMonat(6, { id: 'k4', kanal: 'instagram', serie: true }),
  ], VIEL_PLATZ);
  const raster = dokument.getElementById('kalender-raster');
  const karte = (id) => raster.querySelector('[data-kal-id="' + id + '"]');
  assert.equal(karte('k1').draggable, true);
  assert.notEqual(karte('k2').draggable, true);
  assert.notEqual(karte('k3').draggable, true);
  assert.notEqual(karte('k4').draggable, true);
});
