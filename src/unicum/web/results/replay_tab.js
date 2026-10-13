// A fifth tab on the battle results screen, showing the battle on its map.
//
// Run by the loader (res/gui/gameface/mods/unicum/TankButton/TankButton.js),
// added to the results view by src/unicum/battle_results.py, as the body of a
// function of `api` = { model, media, playSound, report }; returns { stop() }.
//
// WHAT THIS IS AND IS NOT
//
// It is not the site's viewer embedded. It cannot be: this screen is Coherent
// Gameface, which has no iframes at all -- probed, and even `about:blank` is
// refused -- and hands a script an empty body for any response that crosses an
// origin, with or without CORS. Both measured rather than assumed.
//
// So Python fetches the replay from the site and leaves it in the model's
// `replay` property, already decoded server-side and thinned to one position a
// second (`?every=1`, 100 KB instead of 730). What is left here is drawing,
// which is the part that could never have been shared anyway: the site draws
// with React and this page is a bare DOM.
//
// WHY A CLONE OF AN EXISTING TAB
//
// The real tabs carry the game's background, borders, spacing and states, and
// reproducing those by hand is a second job that breaks on the next client
// build. Cloning "Financial Report" inherits all of it for free; only the
// label and the click handler are ours. Probed too: a cloned sibling survives
// React's redraws, including a switch between tabs.

const { model, report } = api;

const TAB_ANCHOR = '[data-test-id="financialReportTab"]';
const TAB_ID = "unicumReplayTab";
const PANEL_ID = "unicumReplayPanel";
const POLL_MS = 250;
// The map is square; this is its side in pixels at the layout's own scale.
const MAP = 520;
// How long one step of playback lasts, and how far it advances. Three seconds
// of battle per tick keeps a six-minute game under a minute to watch.
const STEP_MS = 120;
const STEP_TICKS = 300;

const state = {
  alive: true,
  raw: null,
  replay: null,
  tab: null,
  panel: null,
  open: false,
  at: 0,
  timer: null,
};

function log(what) {
  try {
    report(`replay tab: ${what}`);
  } catch (error) {
    /* the loader is gone; nothing to do about it here */
  }
}

// --------------------------------------------------------------- the drawing

/** Where a point sits on the map, 0..1, from the arena's own metre bounds.
 *
 * `bottomLeft` / `upperRight` is the shape the API already uses for a battle,
 * so the two readers agree. The vertical is flipped because the arena counts
 * metres upward and a picture counts pixels downward.
 */
function place(point, bounds) {
  const [, x, y] = point;
  const w = bounds.upperRight.x - bounds.bottomLeft.x || 1;
  const h = bounds.upperRight.z - bounds.bottomLeft.z || 1;
  return [(x - bounds.bottomLeft.x) / w, 1 - (y - bounds.bottomLeft.z) / h];
}

/** The last position of a vehicle at or before `at`, or null before it starts. */
function positionAt(track, at) {
  const points = track.points || [];
  let found = null;
  for (const point of points) {
    if (point[0] > at) break;
    found = point;
  }
  return found;
}

function draw() {
  const replay = state.replay;
  if (!replay || !state.panel) return;
  const canvas = state.panel.querySelector(".unicum-replay-map");
  if (!canvas) return;
  const bounds = replay.bounds;
  const dots = [];
  for (const track of replay.tracks || []) {
    const point = positionAt(track, state.at);
    if (!point) continue;
    const dead = track.diedAt !== null && track.diedAt <= state.at;
    const [fx, fy] = place(point, bounds);
    dots.push(
      `<div class="unicum-replay-dot${dead ? " is-dead" : ""}${
        track.ally ? " is-ally" : " is-enemy"
      }" style="left:${(fx * 100).toFixed(2)}%;top:${(fy * 100).toFixed(2)}%"></div>`,
    );
  }
  canvas.innerHTML = dots.join("");
  const clock = state.panel.querySelector(".unicum-replay-clock");
  if (clock) {
    const seconds = Math.max(0, Math.round(state.at / replay.ticksPerSecond));
    clock.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  }
}

function step() {
  if (!state.alive || !state.replay) return;
  state.at += STEP_TICKS;
  const end = state.replay.end;
  if (state.at > end) state.at = 0;
  draw();
}

// ----------------------------------------------------------------- the panel

function buildPanel() {
  const panel = document.createElement("div");
  panel.id = PANEL_ID;
  panel.className = "unicum-replay-panel";
  panel.innerHTML = `
    <div class="unicum-replay-frame" style="width:${MAP}px;height:${MAP}px">
      <img class="unicum-replay-ground" />
      <div class="unicum-replay-map"></div>
    </div>
    <div class="unicum-replay-bar">
      <span class="unicum-replay-clock">0:00</span>
      <span class="unicum-replay-note"></span>
    </div>`;
  return panel;
}

function showPanel(show) {
  state.open = show;
  if (state.panel) state.panel.style.display = show ? "" : "none";
  if (state.tab) state.tab.classList.toggle("unicum-replay-active", show);
  if (show) {
    if (!state.timer) state.timer = setInterval(step, STEP_MS);
  } else if (state.timer) {
    clearInterval(state.timer);
    state.timer = null;
  }
}

function fill() {
  if (!state.panel) return;
  const note = state.panel.querySelector(".unicum-replay-note");
  if (!state.replay) {
    if (note) note.textContent = "Waiting for the battle to reach unicum.gg...";
    return;
  }
  if (note) note.textContent = "";
  const ground = state.panel.querySelector(".unicum-replay-ground");
  if (ground && state.replay.map) ground.setAttribute("src", state.replay.map);
  draw();
}

// ------------------------------------------------------------------- the tab

/**
 * Put `text` where the cloned tab keeps its own.
 *
 * The first `span` or `div` is NOT it: the tab nests several, and the one that
 * carries the words is deeper. Writing to the wrong one left the clone reading
 * "FINANCIAL REPORT" beside the real Financial Report -- which is what it did,
 * and only looking at the screen showed it.
 *
 * So the text nodes are what get rewritten: the first one that holds anything
 * becomes the label, the rest are emptied, which also covers a tab whose words
 * are split across nodes.
 */
function relabel(node, text) {
  // Walked by hand rather than with a TreeWalker: this engine has no
  // `document.createTreeWalker`, which it reports only at the moment of the
  // call. Cohtml implements a subset of the DOM and the gaps are found by
  // running into them, so plain recursion is the safer tool here.
  const found = [];
  const walk = (current) => {
    for (const child of Array.from(current.childNodes || [])) {
      if (child.nodeType === 3) {
        if ((child.nodeValue || "").trim()) found.push(child);
      } else {
        walk(child);
      }
    }
  };
  walk(node);
  if (!found.length) {
    node.textContent = text;
    return;
  }
  found[0].nodeValue = text;
  for (let i = 1; i < found.length; i += 1) found[i].nodeValue = "";
}

function mount() {
  if (document.getElementById(TAB_ID)) return true;
  const anchor = document.querySelector(TAB_ANCHOR);
  if (!anchor || !anchor.parentElement) return false;

  const tab = anchor.cloneNode(true);
  tab.id = TAB_ID;
  tab.setAttribute("data-test-id", TAB_ID);
  relabel(tab, "REPLAY");
  anchor.parentElement.appendChild(tab);
  state.tab = tab;

  // The panel covers the content area rather than living inside it: the four
  // real tabs are React's and swapping their content would mean fighting its
  // reconciliation on every render. A layer of our own answers to nobody.
  const panel = buildPanel();
  document.body.appendChild(panel);
  state.panel = panel;
  showPanel(false);

  tab.addEventListener("click", (event) => {
    event.stopPropagation();
    showPanel(!state.open);
  });
  // Any real tab closes ours: the player asked for that screen instead.
  for (const other of Array.from(anchor.parentElement.children)) {
    if (other !== tab) other.addEventListener("click", () => showPanel(false));
  }
  log("mounted");
  fill();
  return true;
}

// ------------------------------------------------------------------ the loop

function readModel() {
  const current = model.model;
  const raw = current && current.replay;
  if (!raw || raw === state.raw) return;
  state.raw = raw;
  try {
    const data = JSON.parse(raw);
    const tracks = data.tracks || [];
    // Ours is the team of the vehicle the replay carries a loadout for: that
    // loadout only ever belongs to the player who recorded. Everything on that
    // side is an ally, the rest the enemy.
    const mine = (data.loadouts || [])[0];
    const ourTeam = mine
      ? (tracks.find((t) => t.id === mine.vehicle) || {}).team
      : undefined;
    for (const track of tracks) {
      track.ally = ourTeam === undefined ? false : track.team === ourTeam;
    }
    // The arena's extent, so a position in metres becomes a place on the
    // picture. Taken from the payload when it carries one and bounded by the
    // points themselves otherwise, which keeps the map honest rather than
    // guessing at a scale.
    const xs = [];
    const ys = [];
    for (const track of tracks) {
      for (const point of track.points || []) {
        xs.push(point[1]);
        ys.push(point[2]);
      }
    }
    const pad = 40;
    state.replay = {
      ticksPerSecond: data.ticksPerSecond || 100,
      tracks,
      map: data.mapImage || null,
      // The arena's own extent when the answer carries one, which it does
      // now; the points' own box otherwise, so an older server still draws
      // something rather than nothing.
      bounds: data.mapBounds || {
        bottomLeft: { x: Math.min.apply(null, xs) - pad, z: Math.min.apply(null, ys) - pad },
        upperRight: { x: Math.max.apply(null, xs) + pad, z: Math.max.apply(null, ys) + pad },
      },
      end: tracks.reduce((end, track) => {
        const points = track.points || [];
        const last = points.length ? points[points.length - 1][0] : 0;
        return Math.max(end, last);
      }, 0),
    };
    state.at = 0;
    log(`got ${tracks.length} track(s)`);
    fill();
  } catch (error) {
    log(`bad replay: ${error}`);
    state.replay = null;
  }
}

function poll() {
  if (!state.alive) return;
  readModel();
  // React rebuilds this screen more than once while it settles, and our tab
  // goes with it when it does. Cheap to check, and the alternative is a tab
  // that is there on one battle and missing on the next.
  if (!document.getElementById(TAB_ID)) {
    state.tab = null;
    mount();
  }
}

const timer = setInterval(poll, POLL_MS);
poll();

return {
  stop() {
    state.alive = false;
    clearInterval(timer);
    if (state.timer) clearInterval(state.timer);
    for (const id of [TAB_ID, PANEL_ID]) {
      const node = document.getElementById(id);
      if (node && node.parentElement) node.parentElement.removeChild(node);
    }
  },
};
