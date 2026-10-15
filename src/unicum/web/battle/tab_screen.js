// Ratings and language flags beside the names of the battle Tab screen.
//
// Run by the loader (res/gui/gameface/mods/unicum/TankButton/TankButton.js),
// grafted onto the Tab view by src/unicum/tab_screen.py, as the body of a
// function of `api` = { model, media, playSound, report }; returns { stop() }.
//
// The model it reads:
//
//   { shown,
//     players:  { name: { image, width, height, flags: [image url] } },
//     averages: { allies: { image, width, height } | null, enemies: ... } }
//
// WHY THE DOM AND NOT THE VIEW'S OWN MODEL
//
// That model was the obvious road and it does not work. A rating was written
// into `userName`, then into `clanAbbrev`, two unrelated string properties
// the page demonstrably binds; a probe read both back out of the native model
// and found them there; neither appeared on screen. So this draws into the
// page instead, which is the only road that can be watched from the inside.
//
// HOW A ROW IS MATCHED TO A PLAYER
//
// By the name it draws, and there is no alternative: the rows carry no
// identifier whatsoever. `data-test-id` appears zero times in the whole
// bundle, and the vehicle id and session id exist only as un-substituted
// binding templates (`{{item.value.vehicleId}}`), so reading those attributes
// yields the template, not a value. Python publishes the roster keyed by
// name, under both the real name and an anonymised player's made-up one, so
// whichever the row shows finds the right account.
//
// WHY A POLL
//
// The rows are not React's here; they are rebuilt by cohtml's own
// data-binding whenever python calls `playerList.invalidate()`, which happens
// on every kill, every reload and every platoon invite. Whether cohtml
// reports its own mutations to a MutationObserver is not established, so this
// polls, as the mod's two other injected scripts already do.

const { model, report } = api;

const ALLIES = '[class*="FullStatsApp_allies_"]';
const ENEMIES = '[class*="FullStatsApp_enemies_"]';
const HEADER = '[class*="FullStatsApp_fullStats_header_"]';
const ROW = '[class*="Player_base_"]';
// A direct child, so the copy of the name inside the tooltip body and the
// anonymiser icon -- both of which carry a `tooltip_trigger` class of their
// own -- are not mistaken for the row's name.
const NAME = '[class*="UserInfo_userName_"] > [class*="UserInfo_tooltip_trigger_"]';
// What shares the allies gutter: the small icons for top damage, top assist
// and top block. `LiveTag_liveTag__wrapper_*` is `position: absolute;
// left: -61rem` and 52rem wide, so it covers that gutter almost exactly, and
// its binding is `{{liveTagDamage}} || {{liveTagAssist}} || {{liveTagBlock}}`
// -- no condition on the reader, so it lands on any ally row leading one of
// the three. It is a child of the row, and only on the allies side.
const LIVE_TAG = '[class*="LiveTag_liveTag"]';
const MARK = "unicum-tab-rating";
const POLL_MS = 200;
// Between a badge and whatever it stands clear of, in pixels.
const GAP = 16;

const state = {
  alive: true,
  raw: null,
  players: {},
  averages: {},
  shown: false,
  clear: 0,
  told: false,
};

function log(what) {
  try {
    report(`tab screen: ${what}`);
  } catch (error) {
    /* the loader is gone; nothing to do about it here */
  }
}

// A rectangle comes back in PIXELS and this page's styles are in rem, so a
// position read off one cannot be written to the other. `viewEnv.pxToRem` is
// the client's own conversion and is what battle_results.js has always used;
// skipping it put every badge off screen at a plausible-looking number.
const toRem = (px) =>
  typeof viewEnv !== "undefined" && viewEnv.pxToRem ? viewEnv.pxToRem(px) : px;

// One layer over the page holds every badge.
//
// NOT a child of its row, which is what this was first: the enemies list is
// exactly as wide as its rows, so a badge past its end falls outside the
// scrolling container and that container cuts it down to a sliver. Padding
// that container admitted the badge and pushed every row out of line with the
// header above it, the header being the container's SIBLING. A layer on the
// body answers to neither, and it is what the results screen already does.
const layer = document.createElement("div");
layer.className = "unicum-tab-layer";
document.body.appendChild(layer);

// ------------------------------------------------------------------ the rows

/** The name a row draws, or "". */
function nameOf(row) {
  const node = row.querySelector(NAME);
  return node ? (node.textContent || "").trim() : "";
}

/** One team's rows, in the order python filled the array. */
function rowsOf(selector) {
  const team = document.querySelector(selector);
  if (!team) return [];
  return Array.from(team.querySelectorAll(ROW));
}

/** The two header blocks, allies first, as the page lays them out. */
function headings() {
  const header = document.querySelector(HEADER);
  if (!header) return {};
  const blocks = Array.from(header.children || []);
  return { allies: blocks[0], enemies: blocks[1] };
}

/** How far left of its row the live tag block reaches, in pixels, or 0.
 *
 * MEASURED, not computed from the stylesheet. The block is declared at
 * `left: -61rem` but it only takes space when the player leads damage,
 * assist or block, and reading `data-bind-if` state from the outside is
 * guesswork -- an earlier attempt tested for the element's presence and was
 * wrong about when it is in the DOM at all. A rectangle is the fact.
 */
function reach(row) {
  const tag = row.querySelector(LIVE_TAG);
  if (!tag) return 0;
  const over = tag.getBoundingClientRect();
  if (!over.width) return 0;
  const mine = row.getBoundingClientRect();
  return Math.max(0, Math.round(mine.left - over.left));
}

// ---------------------------------------------------------------- the badges

/** The badge for one row of one side, created on first use.
 *
 * Kept by side and position rather than against the row: cohtml rebuilds
 * these rows from scratch on every `playerList.invalidate()`, so an element
 * held against one would be thrown away with it several times a battle.
 */
const badges = {};

function badgeOf(key, side) {
  let badge = badges[key];
  if (!badge) {
    badge = document.createElement("div");
    badge.className = `${MARK} ${MARK}--${side}`;
    // Nowhere yet: `place` shows it once it has a rectangle to sit beside,
    // so a badge is never seen in the corner it was created in.
    badge.style.display = "none";
    layer.appendChild(badge);
    badges[key] = badge;
  }
  return badge;
}

/** 3323 -> "3 323": grouped by thousands, as every other badge the mod draws.
 *
 * Only the fallback needs it, since the image carries its own digits, but it
 * has to read the same way when it stands in.
 */
function group(value) {
  const digits = String(value);
  let out = "";
  for (let i = 0; i < digits.length; i += 1) {
    if (i > 0 && (digits.length - i) % 3 === 0) out += " ";
    out += digits[i];
  }
  return out;
}

/** Draw a rating into `node`: the badge image, or the drawn fallback.
 *
 * The IMAGE when python could make one, which is the ordinary case: it is
 * the same PNG the mod puts above the vehicles and in the players panel, on
 * `badge_png`'s fixed grid -- 3px of padding, a 6px cell per digit, 2px
 * between thousands -- so the number reads identically wherever it appears.
 * Imitating that grid in CSS was tried twice and stayed visibly not the same
 * badge. `img://` resolves in this page: the language flags beside these
 * badges are already drawn that way.
 *
 * The digits are the fallback for the one session where the badge folder was
 * missing from the client's resource index at startup, which is the only
 * case `Badges.image` cannot answer.
 */
function fill(node, rating) {
  const image = rating && rating.image ? rating.image : "";
  if (node.getAttribute("data-unicum-image") !== image) {
    node.setAttribute("data-unicum-image", image);
    node.style.backgroundImage = image ? `url(${image})` : "";
    node.style.width = image ? `${rating.width}rem` : "";
    node.style.height = image ? `${rating.height}rem` : "";
  }
  const text = !image && rating && rating.value !== undefined ? group(rating.value) : "";
  if (node.textContent !== text) node.textContent = text;
  const color = !image && rating && rating.color ? rating.color : "";
  if (node.style.backgroundColor !== color) node.style.backgroundColor = color;
}

/** The rating of a badge, in its own node so the flags beside it survive. */
function setValue(badge, rating) {
  let node = badge.querySelector(`.${MARK}_value`);
  if (!node) {
    node = document.createElement("div");
    node.className = `${MARK}_value`;
    // Before the flags, which read outwards from the table.
    badge.insertBefore(node, badge.firstChild);
  }
  fill(node, rating);
}

/** Put `urls` in the badge's flag strip, rebuilt only when it changes. */
function setFlags(badge, urls) {
  const wanted = (urls || []).join(" ");
  if (badge.getAttribute("data-unicum-flags") === wanted) return;
  badge.setAttribute("data-unicum-flags", wanted);
  let strip = badge.querySelector(`.${MARK}_flags`);
  if (!wanted) {
    if (strip && strip.parentElement) strip.parentElement.removeChild(strip);
    return;
  }
  if (!strip) {
    strip = document.createElement("div");
    strip.className = `${MARK}_flags`;
    badge.appendChild(strip);
  }
  strip.innerHTML = "";
  for (const url of urls) {
    const flag = document.createElement("div");
    flag.className = `${MARK}_flag`;
    flag.style.backgroundImage = `url(${url})`;
    strip.appendChild(flag);
  }
}

/** Put `badge` beside the rectangle `rect`, in the layer's coordinates.
 *
 * `left` and `top` only, each converted once at the end, which is exactly
 * what `battle_results.js` does on the results screen. An earlier version
 * set `right` instead, to save measuring the badge's own width, and that
 * width is the only thing `right` saves: everything else about it -- which
 * box its percentage resolves against, what it means inside a layer -- is
 * one more thing to be wrong about, and it was, twice. `offsetWidth` and
 * `offsetHeight` read back in the same pixel space as the rectangle, so the
 * whole sum is done in pixels and converted once at the end.
 */
function place(badge, rect, side, clear) {
  if (!rect || !rect.height || !rect.width) return;
  const left =
    side === "allies" ? rect.left - GAP - clear - badge.offsetWidth : rect.right + GAP;
  const top = rect.top + (rect.height - badge.offsetHeight) / 2;
  const x = `${Math.round(toRem(left))}rem`;
  const y = `${Math.round(toRem(top))}rem`;
  if (badge.style.left !== x) badge.style.left = x;
  if (badge.style.top !== y) badge.style.top = y;
  if (badge.style.display === "none") badge.style.display = "";
}

// ------------------------------------------------------------------- drawing

// Which badges this pass still has somewhere to be.
const alive = {};

function decorate() {
  let drawn = 0;
  let matched = 0;
  for (const [selector, side] of [
    [ALLIES, "allies"],
    [ENEMIES, "enemies"],
  ]) {
    const rows = rowsOf(selector);
    // One offset for the whole column, the widest any row needs. Per-row
    // offsets would clear the icons just as well and leave the ratings in a
    // ragged line, which is worse to read than a column standing slightly
    // further out.
    let clear = 0;
    if (side === "allies") {
      for (const row of rows) clear = Math.max(clear, reach(row));
      state.clear = clear;
    }
    for (let index = 0; index < rows.length; index += 1) {
      const row = rows[index];
      const key = `${side}:${index}`;
      alive[key] = true;
      const badge = badgeOf(key, side);
      drawn += 1;
      const name = nameOf(row);
      const player = state.shown && name ? state.players[name] : null;
      if (player) matched += 1;
      setValue(badge, player);
      setFlags(badge, player ? player.flags : null);
      place(badge, row.getBoundingClientRect(), side, clear);
    }
  }
  return { drawn, matched };
}

function drawAverages() {
  const blocks = headings();
  for (const side of ["allies", "enemies"]) {
    const block = blocks[side];
    const average = state.shown ? state.averages[side] : null;
    if (!block || !average) continue;
    const key = `${side}:mean`;
    alive[key] = true;
    const badge = badgeOf(key, side);
    setValue(badge, average);
    // The header block is sized to its own list -- 570rem with a 69rem left
    // margin for the allies, 530rem for the enemies -- so its edges are the
    // rows' edges, and placing the average off it puts it in the very column
    // the ratings below are standing in, live tag offset included.
    place(badge, block.getBoundingClientRect(), side, state.clear);
  }
}

// ------------------------------------------------------------------ the data

function readModel() {
  const current = model.model;
  const raw = current && current.data;
  if (!raw || raw === state.raw) return;
  state.raw = raw;
  try {
    const data = JSON.parse(raw);
    state.players = data.players || {};
    state.averages = data.averages || {};
    state.shown = !!data.shown;
  } catch (error) {
    log(`bad data: ${error}`);
  }
}

// ------------------------------------------------------------------- saying so
//
// One line per screen. This one is kept rather than taken out with the rest
// of the scaffolding: three attempts to reach this screen through its model
// were written, confirmed present in the model and invisible on screen, and
// two placements after that put the badges off the page. None of those would
// have been visible in a log that said nothing, and the only cost of this is
// one line each time the Tab is built.

function told(counts) {
  if (state.told || !counts.matched) return;
  state.told = true;
  log(`${counts.drawn} row(s), ${counts.matched} rated`);
}

// ------------------------------------------------------------------ the loop

function poll() {
  if (!state.alive) return;
  readModel();
  for (const key of Object.keys(alive)) delete alive[key];
  const counts = decorate();
  drawAverages();
  // A row or a header that went away keeps its badge in the layer; hide it
  // rather than leave it standing where that row used to be.
  for (const key of Object.keys(badges)) {
    if (!alive[key]) badges[key].style.display = "none";
  }
  told(counts);
}

const timer = setInterval(poll, POLL_MS);
poll();

return {
  stop() {
    state.alive = false;
    clearInterval(timer);
    if (layer.parentElement) layer.parentElement.removeChild(layer);
  },
};
