// The timing tower, the pop-ins, the fastest-lap toast and the instant-replay wipe:
// everything broadcast-overlay.html does once a model arrives. One IIFE; the sections are
// marked with `// ---` rules. Out of the HTML so it can be loaded under a headless-Chrome
// harness (tests/test_overlay_js.py) that feeds render() hand-built models and asserts
// the DOM, which nothing could do while the script was inline and its functions were
// closed over. Served by overlay/transport.py with the same no-store headers as the page.

(() => {
  "use strict";

  const params = new URLSearchParams(location.search);
  if (params.has("live")) {
    document.body.classList.add("live");
    document.documentElement.classList.add("live");
  }

  // Channel bug: the operator's own logo, from the settings (look.logo) by way of
  // /show.json, or ?bug=<url> to override it for one source. ?bug= (empty) turns it
  // off, as does look.bug = false. A missing or broken asset removes the element
  // rather than leaving a broken-image glyph on air for a whole race.
  //
  // The CHIP goes, not just the image inside it: the mark sits on a glass plate, and
  // dropping the image alone would leave an empty box in the corner all race.
  // iRacing's own logo, which Pylon does not ship (it is their trademark). Present
  // in overlays/brand/, it draws; absent, the whole block goes rather than leaving a
  // broken-image glyph in the corner of the broadcast for two hours.
  //
  // The already-failed case is the one that actually happens. This src is in the
  // HTML, so the request is made and lost while the parser is still working; by the
  // time this script runs at the end of <body> the error event has FIRED AND GONE,
  // and a listener added now never hears it. A bordered box reading "iRacing" sat
  // in the corner of every frame until that was understood, which on air is two
  // hours of a broken-image glyph. `complete` with a zero naturalWidth is how a
  // finished-and-failed image reads.
  {
    const sim = document.querySelector(".simbug img");
    const drop = () => { (sim.closest(".simbug") || sim).remove(); };
    if (sim) {
      sim.addEventListener("error", drop);
      if (sim.complete && sim.naturalWidth === 0) drop();
    }
  }

  function applyBug(src) {
    const bug = document.querySelector(".bug");
    if (!bug) return;
    const chip = bug.closest(".bugchip") || bug;
    bug.addEventListener("error", () => chip.remove());
    if (!src) { chip.remove(); return; }
    bug.src = src;
    // Same already-failed hazard as the sim bug above: a cached 404 can settle
    // before the listener is reached.
    if (bug.complete && bug.naturalWidth === 0) chip.remove();
  }

  // Tower geometry comes from the stylesheet rather than from a second copy of it. These
  // three numbers have to agree with the CSS exactly (the scroll maths below decides
  // which rows fall inside the window), and WINDOW used to be declared once in each
  // language with a comment asking the next reader to keep them in step by hand. The
  // fallbacks are only for a stylesheet that failed to load at all.
  const cssNum = (name, fallback) => {
    const v = parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name));
    return Number.isFinite(v) && v > 0 ? v : fallback;
  };
  const ROW_H = cssNum("--rowH", 42);
  const PINNED = cssNum("--pinned", 5);    // frozen leaders
  const WINDOW = cssNum("--window", 16);   // MOST rows the follow-cam window ever shows

  const els = {
    stage: document.getElementById("stage"),
    rpyWhat: document.getElementById("rpyWhat"),
    rpyWho: document.getElementById("rpyWho"),
    wipe: document.getElementById("wipe"),
    cap: document.querySelector(".cap"),
    flag: document.getElementById("flag"),
    lap: document.getElementById("lap"),
    lapLabel: document.getElementById("lapLabel"),
    track: document.getElementById("track"),
    session: document.getElementById("session"),
    tower: document.querySelector(".tower"),
    pinned: document.getElementById("pinned"),
    scroll: document.getElementById("scroll"),
    scrollInner: document.getElementById("scrollInner"),
    theadOutrig: document.getElementById("theadOutrig"),
    moreTop: document.getElementById("moreTop"),
    moreBot: document.getElementById("moreBot"),
    popins: document.getElementById("popins"),
    toast: document.getElementById("toast"),
    toastCar: document.getElementById("toastCar"),
    toastTime: document.getElementById("toastTime"),
    theadGap: document.getElementById("theadGap"),
  };

  const FLAG_COLOR = {
    green: "var(--flag-green)", yellow: "var(--flag-yellow)", red: "var(--flag-red)",
    checkered: "var(--flag-checkered)", white: "var(--flag-white)", none: "var(--dim)",
  };
  // No kind LABEL on the pop-ins. "On Camera" over the car the camera is obviously on,
  // "Battle" over two cars nose to tail, "Race Leader" over the car wearing a P1 badge:
  // each one spent the card's most valuable line restating what the picture already said,
  // and the line is worth more as the driver's given name (the shape the F1 world feed
  // uses). The shot kind still drives the card's TREATMENT (.leader, .trouble), which is
  // the part that carried information: an accent position block, a brand-tinted class bar.
  //
  // This also disposes of the "Race Leader in a practice session" problem (#40) more
  // thoroughly than relabelling it did: there is no label left to be wrong.

  const hex = (c) => "#" + ((c >>> 0) & 0xffffff).toString(16).padStart(6, "0");

  // gap column text + style: interval to the car directly ahead (useful in
  // endurance, where to-leader is just "+5 LAPS" for half the field).
  const fmtSecs = (s) => {
    if (s < 60) return "+" + s.toFixed(1);
    const m = Math.floor(s / 60);
    return "+" + m + ":" + (s - m * 60).toFixed(1).padStart(4, "0");
  };
  // PIT no longer lives here. A pit stop is precisely when positions change, so it is
  // when the viewer most wants the gap, and PIT used to replace it outright. It moved
  // to the outrigger (outrigFor), and this column now always shows an actual gap.
  //
  // In a NON-RACE session it shows a lap-time gap instead, because there is no race gap
  // to show: the car on the row above is not one this car is racing, so their on-track
  // separation is two unrelated cars that happen to be near each other. What a practice
  // or qualifying screen shows is the deficit to the quickest car, with the pace-setter
  // showing the time everyone else is measured against. The gap is to the FASTEST rather
  // than to the row above on purpose: in a session where nobody is racing anybody, "how
  // far off the pace" is the fact, and the car above you is arbitrary.
  //
  // A car with no lap yet gets a blank, the same answer the outrigger gives, and for the
  // same reason: that is most of the field for the opening stretch of a session, and an
  // empty cell reads as "nothing to say yet" where a "--" reads as a value to decode.
  function gapFor(c, s) {
    // A car on a flatbed has no interval. Its own class, so the change of state redraws
    // the cell at once: the gap text is otherwise settled at ~1 Hz, and "same class, wait
    // for the tick" would leave the last live gap standing next to TOW for a second.
    if (c.tow) return ["", "is-tow"];
    if (s && s.type && s.type !== "race") {
      if (c.lapGap == null) return ["", ""];
      if (c.lapGap <= 0) return [fmtLap(c.bestLap), "best-tag"];   // the pace-setter
      return ["+" + c.lapGap.toFixed(3), ""];
    }
    if (c.isLeader) return ["LEADER", "leader-tag"];
    if ((c.intervalLaps || 0) >= 1) return ["+" + c.intervalLaps + " L", "lap-tag"];
    return [fmtSecs(c.interval || 0), ""];
  }

  // A lap time the way a timing screen writes it: "1:38.204", or "58.204" under the
  // minute. Three decimals is the motorsport convention and the tower is tabular, so
  // the extra digits cost no layout.
  const fmtLap = (s) => {
    const m = Math.floor(s / 60), rest = s - m * 60;
    return m > 0 ? m + ":" + rest.toFixed(3).padStart(6, "0") : rest.toFixed(3);
  };
  // The outrigger column: PIT, else the last completed lap, else nothing.
  //
  // `lastLap` is null (never 0, never -1) for a car that has not completed a lap:
  // overlay.py drops iRacing's -1.0 sentinel before it ever reaches the wire. The
  // `> 0` here is belt-and-braces for a hand-built or older model, NOT the real
  // filter; see DESIGN.md 14 for why this project distrusts these two channels.
  // Blank beats a placeholder: an empty slot in a quiet column reads as "nothing to
  // say yet", where a "--" reads as a value the viewer has to decode.
  // A flag shown to ONE car outranks everything else the outrigger could say, PIT
  // included: a black-flagged car is not a lap time, and the meatball is an ORDER to
  // come in, so replacing PIT with it loses nothing a viewer needed. overlay.py hands
  // over a token, never iRacing's bitfield, and "" for the normal state, which is
  // almost every car almost always.
  const PENALTY = {
    dsq: ["DSQ", "is-black"],
    black: ["BLACK", "is-black"],
    repair: ["REPAIR", "is-meatball"],   // the meatball: damage, not misconduct
    warn: ["WARN", "is-furled"],         // furled black: a warning, not yet a penalty
  };
  // In QUALIFYING the column holds the car's BEST lap rather than its last, and says
  // "Best" (#54). The best lap IS the result there, while a last lap is as likely to be an
  // in lap, an out lap or an aborted run, so the column spent the session showing a
  // number that meant nothing while the number that decides it was nowhere on screen.
  //
  // PRACTICE DELIBERATELY KEEPS THE LAST LAP. There the last lap is the useful one: it
  // says who is on a hot lap right now and whether they are improving. The neighbouring
  // gate (`by_lap` in overlay.py, and the "Gap" header above) covers practice AND
  // qualifying together, and bestLap is populated for both, so reaching for that
  // predicate here would silently change practice as well. This tests the session kind
  // itself. Making practice follow needs a decision, not a default.
  //
  // GARAGE sits between PIT and the lap time and only ever replaces a LAST lap: a car in
  // the garage is not on a lap at all, so its last one is a number it has stopped
  // improving on. It never replaces a best, that is the time the car is classified on,
  // and hiding it would take the session's own result off the board. The row goes quiet
  // either way (see .row.garage).
  function outrigFor(c, bestCol) {
    const p = c.penalty && PENALTY[c.penalty];
    if (p) return p;
    if (c.tow) return ["TOW", "is-tow"];        // out of the world in a race; see .row.tow
    if (c.onPit) return ["PIT", "is-pit"];      // a live state, and it still matters in qualy
    if (bestCol) return [c.bestLap > 0 ? fmtLap(c.bestLap) : "", ""];
    if (c.garage) return ["GARAGE", "is-garage"];
    return [c.lastLap > 0 ? fmtLap(c.lastLap) : "", ""];
  }

  // ---- fastest lap: the loud transient, when the time CHANGES HANDS ----
  //
  // The row's purple marker carries the steady state, so the toast is spent only on the
  // moment: someone has taken the fastest lap off someone else. Deliberately silent for
  // the FIRST holder of a session, because that is also what a mid-session connect looks
  // like: the OBS browser source reloads and reconnects, and a toast fired then would
  // announce a lap set ten minutes ago as news. A holder improving their OWN time is not
  // a change of hands and passes quietly; the tower still shows the new number.
  const TOAST_MS = 5200;
  let fastHolder = null;      // car id, once a holder has been seen at all
  let toastTimer = null;

  function fastestToast(cars, sessionFastest) {
    const holder = cars.find((c) => c.fastest);
    const id = holder ? holder.id : null;
    if (id === fastHolder) return;
    const first = fastHolder === null;
    fastHolder = id;
    if (first || id === null || sessionFastest == null) return;
    els.toastCar.textContent = "#" + holder.num + " " + holder.tla;
    els.toastTime.textContent = fmtLap(sessionFastest);
    els.toast.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => els.toast.classList.remove("show"), TOAST_MS);
  }

  // session sub-line: a LIVE/REPLAY badge + the event clock (time remaining if the
  // race is timed, else elapsed). Driven by iRacing's IsReplayPlaying + SessionTime*.
  // the session label is the one piece of raw iRacing text that reaches innerHTML
  // (an unclassified SessionType is printed as-is), so it goes through this first
  const esc = (s) => String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  const fmtClock = (sec) => {
    sec = Math.max(0, Math.round(sec));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
  };
  // For a timed race the big cap block shows the countdown (see render), so the sub-line
  // carries the lap count; for a lap race the big block shows the lap, so the sub-line
  // carries elapsed time. Either way the badge (LIVE/REPLAY) leads, and the session
  // badge (PRACTICE / QUALIFYING / RACE) sits next to it: a practice tower has the same
  // rows and gaps as a race one, so nothing else on screen says these cars are not racing.
  // The show's own identity, fetched once. It is a constant for the whole session
  // (round seven is round seven all afternoon) so it does NOT ride the per-frame
  // model: that is a wire format for things that change, and putting a fixed string
  // in every one of fifteen pushes a second would be paying for it over and over.
  // /show.json is served by overlay/transport.py out of the operator's config, and
  // is `{}` on a machine where nothing has been configured, which is a working show
  // and not an error.
  let showRound = "";
  fetch("show.json", { cache: "no-store" })
    .then((r) => (r.ok ? r.json() : null))
    .then((doc) => {
      doc = doc || {};
      showRound = doc.round || "";
      if (doc.colour) {
        // One colour in the settings; the lift and the deep are mixed from it, so
        // a pale brand stays readable on glass and a dark one does not vanish.
        const root = document.documentElement.style;
        root.setProperty("--brand", doc.colour);
        root.setProperty("--brand-lift", `color-mix(in srgb, ${doc.colour} 65%, white)`);
        root.setProperty("--brand-deep", `color-mix(in srgb, ${doc.colour} 60%, black)`);
      }
      applyBug(params.has("bug") ? params.get("bug")
                                 : (doc.bug === false ? "" : (doc.logo || "")));
      if (doc.tower === false) document.body.classList.add("no-tower");
    })
    .catch(() => { applyBug(params.has("bug") ? params.get("bug") : ""); });

  function roundBadge() {
    if (!showRound) return "";
    return `<span class="badge badge--round">${esc(showRound)}</span>`;
  }

  function sessionSub(s) {
    // LIVE, or nothing at all. There is no REPLAY badge here any more.
    //
    // It used to read REPLAY for a saved tape, which is true but useless and now
    // actively harmful: the INSTANT REPLAY treatment means something specific and
    // temporary, and a permanent REPLAY chip in the corner of a six-hour tape trains
    // the viewer to ignore exactly the word we need them to notice. LIVE is the claim
    // worth making, so it is the only one made; its absence says the rest.
    const badge = s.replay
      ? ""
      : `<span class="badge badge--live"><i></i>LIVE</span>`;
    const session = s.typeLabel
      ? `<span class="badge badge--session${s.type && s.type !== "race" ? " is-nonrace" : ""}">`
        + `${esc(s.typeLabel)}</span>`
      : "";
    let info = "";
    if (s.timeRemaining != null) {
      // No lap number means a session where a lap COUNT is not a thing: in practice or
      // qualifying every car is on its own lap and the top row's is nobody else's, so
      // overlay.py sends no lap at all. A "3 to go" hanging off nothing would be worse
      // than the blank: the big block already carries the clock, which is the real fact.
      info = s.lap ? `LAP ${s.lap}` : "";
      if (info && s.lapsRemaining != null && s.lapsRemaining > 0) {
        info += ` <em>· ${s.lapsRemaining} to go</em>`;
      }
    } else if (s.elapsed != null) {
      info = `${fmtClock(s.elapsed)} <em>elapsed</em>`;
    }
    return badge + session + roundBadge()
           + (info ? `<span class="cap__clock">${info}</span>` : "");
  }

  // rows are created on first sight of a car, keyed by id, then reused
  const rows = new Map();
  // The interval column is settled to ~1 Hz: a 15 Hz relative jitters and is unreadable.
  // (Row positions still animate every frame; only the number text is throttled.)
  const GAP_TICK_MS = 1000;
  let gapTickAt = 0;
  function ensureRow(car) {
    let r = rows.get(car.id);
    if (r) return r;
    const el = document.createElement("div");
    el.className = "row";
    el.innerHTML =
      '<span class="pos"><b></b></span>' +
      '<span class="num"></span>' +
      '<span class="tla"></span>' +
      '<span class="gap"></span>' +
      '<span class="outrig"></span>' +
      '<span class="row__cam"></span>';
    r = {
      el, parent: null,
      num: el.querySelector(".num"), tla: el.querySelector(".tla"),
      pos: el.querySelector(".pos b"),
      gap: el.querySelector(".gap"), outrig: el.querySelector(".outrig"), key: null,
    };
    rows.set(car.id, r);
    return r;
  }

  // --- where the follow-cam window sits ---------------------------------------
  // The window used to be recomputed from scratch every frame as "centre the on-cam
  // car", which made the tower the most restless thing in the frame: a cut from P6 to
  // P21 slid fifteen rows under the viewer in half a second and the next cut slid them
  // back. The tower is the one element on screen that is supposed to be a stable
  // reference. So its position is now persistent state that PAGES toward the on-cam car
  // instead of tracking it, and does not move at all while that car is already in view.
  const STEP = 10;         // rows the window pages by
  const DWELL_MS = 4000;   // minimum hold between pages
  // A car swapping position back and forth across the window's edge must not page the
  // tower every dwell, so a move has to be asked for CONTINUOUSLY for this long before
  // it is committed. (Both of these want tuning against a real race capture.)
  const DEMAND_MS = 750;
  let winStart = PINNED;      // first rank inside the window
  let winPagedAt = -Infinity; // when it last moved
  let winWant = null;         // the target being asked for right now
  let winWantAt = 0;          // ...and since when
  // Called once per telemetry frame (never on a timer: a timer would page the tower
  // against stale rows while the stream is stalled or reconnecting), and returns the
  // first rank the window shows. `winRows` is the window's CURRENT height, which on a
  // small field is smaller than WINDOW.
  function towerWindow(nowMs, focusRank, winRows, maxStart) {
    // The field shrinks when cars leave, and a start above the new maximum would show
    // empty space below the last row. Clamp every frame, not only when moving.
    winStart = Math.min(Math.max(winStart, PINNED), maxStart);
    let want;
    if (focusRank < PINNED) {
      // On a pinned car, or on nothing at all: settle back onto the head of the field
      // rather than staying parked wherever the last cut left us.
      want = PINNED;
    } else if (focusRank >= winStart && focusRank < winStart + winRows) {
      want = winStart;                     // already in view: hold absolutely still
    } else {
      // the PAGE holding that car. WINDOW is larger than STEP, so it lands comfortably
      // inside the window rather than on its edge.
      want = Math.min(PINNED + Math.floor((focusRank - PINNED) / STEP) * STEP, maxStart);
    }
    if (want !== winWant) { winWant = want; winWantAt = nowMs; }
    if (want === winStart) return winStart;
    if (nowMs - winWantAt < DEMAND_MS) return winStart;   // not asked for long enough
    if (nowMs - winPagedAt < DWELL_MS) return winStart;   // still holding the last page
    // One step, toward the target from wherever we are now: a target that changed
    // mid-trip is steered to, not restarted from.
    winStart = want > winStart ? Math.min(winStart + STEP, want)
                               : Math.max(winStart - STEP, want);
    winPagedAt = nowMs;
    return winStart;
  }

  // --- camera focus pop-in -------------------------------------------------
  // one pop-in card per focused car (two in a battle), reused by car id
  const popinCards = new Map();
  let popinSig = "";
  function makeCard() {
    const el = document.createElement("div");
    el.className = "popin";
    el.innerHTML =
      '<div class="popin__cls"></div>' +
      '<div class="popin__pos">' +
        '<div class="popin__posnum"><span>P</span><b></b></div>' +
        '<div class="popin__poscls"><span class="popin__clschip"></span><span class="popin__clspos"></span></div>' +
      '</div>' +
      '<div class="popin__body">' +
        '<div class="popin__first"></div>' +
        '<div class="popin__name"></div>' +
        '<div class="popin__make"></div>' +
      '</div>' +
      '<div class="popin__creds">' +
        '<span class="popin__lic"></span>' +
        '<span class="popin__ir"></span>' +
        '<span class="popin__country"></span>' +
      '</div>';
    return {
      el, cls: el.querySelector(".popin__cls"), pos: el.querySelector(".popin__posnum b"),
      clschip: el.querySelector(".popin__clschip"), clspos: el.querySelector(".popin__clspos"),
      first: el.querySelector(".popin__first"), name: el.querySelector(".popin__name"),
      make: el.querySelector(".popin__make"), creds: el.querySelector(".popin__creds"),
      lic: el.querySelector(".popin__lic"), ir: el.querySelector(".popin__ir"),
      country: el.querySelector(".popin__country"), key: null,
    };
  }

  // the live gap chip that sits between a battle's two cards
  const gapChip = (() => {
    const el = document.createElement("div");
    el.className = "popin-gap";
    // value over label, the way the reference reads: the number is the graphic and the
    // words underneath only say what it is
    el.innerHTML =
      '<span class="popin-gap__val"></span>' +
      '<span class="popin-gap__label">Current Gap</span>';
    return { el, val: el.querySelector(".popin-gap__val") };
  })();
  // A gap that is coming DOWN goes green, which is the one thing a viewer wants to know
  // about a battle: is it happening. `closing` (seconds per second, +ve = closing) has
  // ridden the payload since the chip was built and nothing ever read it: the CSS said
  // "colour tracks the trend" and no code set a trend. The threshold keeps it off for
  // float noise on a pair running at the same pace.
  const CLOSING_MIN = 0.02;
  function updateGapChip(gap, closing) {
    const v = gap < 10 ? gap.toFixed(1) : String(Math.round(gap));
    gapChip.val.innerHTML = "+" + v + "<i>s</i>";
    gapChip.el.classList.toggle("is-closing", (closing || 0) > CLOSING_MIN);
  }
  // Persistent name bug(s): stay up the whole time the car(s) are on camera, so you
  // always know what you're looking at; a battle shows both cars in the pair.
  function renderPopins(focus) {
    const cars = (focus && focus.cars) || [];
    const ids = cars.map((c) => c.id);
    const sig = focus ? focus.kind + ":" + ids.join(",") : "";
    const structural = sig !== popinSig;
    const isBattle = focus && focus.kind === "battle" && cars.length === 2;
    const hasGap = isBattle && focus.gap != null;

    if (structural) {
      popinSig = sig;
      popinCards.forEach((card, id) => {           // drop cards no longer on camera
        if (!ids.includes(id)) {
          const el = card.el;
          // Out of the FLOW immediately, pinned where it already is, so it fades out
          // without shoving the incoming group sideways. It used to keep its place in the
          // flex row for the whole 500ms fade, so a cut to a new subject laid the new
          // cards out beside the departing one and then snapped them across as it went,
          // which read as the graphic jumping on arrival. `.popins` is positioned, so it
          // is the offsetParent and these offsets pin the card exactly where it sat.
          el.style.left = el.offsetLeft + "px";
          el.style.top = el.offsetTop + "px";
          el.classList.add("leaving");
          el.classList.remove("show");
          // Unconditional: if this car comes back inside the fade it gets a NEW card
          // (popinCards was deleted), so this element is always the stale one. The old
          // guard checked the id instead and left the element in the DOM forever.
          setTimeout(() => el.remove(), 500);
          popinCards.delete(id);
        }
      });
      // Appended in pair order, which IS left-to-right now: overlay._battle_cards hands
      // the pair over leading car first, so the front of the fight lands on the left and
      // the car chasing it on the right, the way a viewer sees them on the road.
      cars.forEach((c) => {
        if (!popinCards.has(c.id)) popinCards.set(c.id, makeCard());
        els.popins.appendChild(popinCards.get(c.id).el);
      });
      // Two cards side by side have to be narrower to clear the tower (see --popinW).
      els.popins.classList.toggle("is-battle", isBattle);
    }
    // The chip's presence is decided EVERY frame, not only on a structural change: a
    // pair's gap can be unknown on the first frame of a shot (no est-lap yet, a lap
    // boundary) and known a frame later, and a chip that was never inserted was being
    // updated in a detached element for the rest of the shot. Slotting it before the
    // second card puts it BETWEEN the pair.
    if (isBattle && popinCards.has(cars[1].id)) {
      const anchor = popinCards.get(cars[1].id).el;
      if (hasGap && gapChip.el.parentNode !== els.popins) els.popins.insertBefore(gapChip.el, anchor);
      else if (!hasGap && gapChip.el.parentNode) gapChip.el.remove();
    } else if (gapChip.el.parentNode) {
      gapChip.el.remove();
    }

    cars.forEach((c) => {
      const card = popinCards.get(c.id);
      // no class colour (single-make field): hand the token back rather than a
      // literal, so --cls keeps tracking the palette instead of pinning to
      // whatever --accent happened to be the day this line was written
      const col = c.classColor ? hex(c.classColor) : "var(--accent)";
      // The P badges say who is where and they are live, so the graphic turns over as the
      // move happens. The old "Defending P7 / Attacking P7" labels are long gone (naming
      // a role forces a call the data cannot make while two cars are genuinely side by
      // side (measured on capture2: 349 ticks inside the 10 m clear-pass deadband)), and
      // now the neutral "Battle" label has followed them, along with every other kind
      // label. See the note by the old KIND_LABEL table.
      const tag = c.carTag || c.className || "";
      const key = c.id + "|" + isBattle + "|" + c.pos + "|" + c.classPos + "|" + c.num +
                  "|" + tag + "|" + (c.carModel || "") +
                  "|" + (c.licClass || "") + (c.licSR || "") + "|" + (c.irating || "") +
                  "|" + (c.country || "");
      card.el.style.setProperty("--cls", col);
      card.cls.style.setProperty("--cls", col);
      card.el.classList.toggle("oncam", !!c.onCam);
      card.el.classList.toggle("rival", !c.onCam);
      card.el.classList.toggle("leader", focus.kind === "leader");
      card.el.classList.toggle("trouble", focus.kind === "trouble");
      if (card.key !== key) {
        card.pos.textContent = c.pos || "-";
        // badge underline: class chip + position in class (fills the space under the number)
        card.clschip.textContent = tag;
        card.clschip.style.display = tag ? "" : "none";
        card.clspos.textContent = c.classPos ? "P" + c.classPos : "";
        // A battle bar splits the name over two lines the way a world feed does: given
        // name small above, SURNAME large in the class colour. The split comes from the
        // payload (overlay._name_parts), not from a last-space guess here: a surname is
        // not the last word when iRacing is carrying a generational suffix. Falls back to
        // the one-line form whenever the parts are missing, which is also the solo card.
        const twoLine = isBattle && (c.last || c.first);
        card.first.textContent = twoLine ? (c.first || "") : "";
        card.name.innerHTML = twoLine
          ? (c.last || "")
          : `<span class="popin__no">#${c.num}</span>` + (c.full || c.tla || "");
        // manufacturer wordmark (first token of the model) + the rest of the model. The
        // battle bar leads it with the car number, which the reference does not carry at
        // all, but ours is the number on the tower row, and this is the card's "which
        // car" line, so it costs nothing to keep them matchable.
        const model = c.carModel || "";
        const make = model.split(/[\s.]+/)[0];
        const num = twoLine ? `<span class="popin__no">#${c.num}</span>` : "";
        card.make.innerHTML = num + (model
          ? (make && model.length > make.length
              ? `<b>${make}</b>${model.slice(make.length)}` : `<b>${model}</b>`)
          : (c.className || ""));
        // credentials: licence chip in iRacing's class colour, iRating, country.
        // Any of the three can be missing (AI, unofficial session): each element
        // hides itself when empty, and the whole column goes when all three are.
        card.lic.innerHTML = c.licClass
          ? `<b>${c.licClass}</b>` + (c.licSR ? `<i>${c.licSR}</i>` : "") : "";
        card.lic.style.setProperty("--lic", c.licColor ? hex(c.licColor) : "var(--dim)");
        card.ir.innerHTML = c.irating ? `${c.irating}<em>iR</em>` : "";
        card.country.textContent = c.flag || c.country || "";
        card.country.classList.toggle("is-name", !c.flag && !!c.country);
        card.country.title = c.country || "";
        card.creds.style.display = (c.licClass || c.irating || c.country) ? "" : "none";
        card.key = key;
      }
      requestAnimationFrame(() => card.el.classList.add("show"));
      if (structural) {                              // re-emphasize on each cut
        card.el.classList.remove("bump");
        void card.el.offsetWidth;
        card.el.classList.add("bump");
      }
    });

    // gap chip: refreshed every frame so the interval ticks live
    if (hasGap) {
      updateGapChip(focus.gap, focus.closing);
      requestAnimationFrame(() => gapChip.el.classList.add("show"));
    } else {
      gapChip.el.classList.remove("show");
    }

    if (!cars.length) popinCards.forEach((card) => card.el.classList.remove("show"));
  }

  // render(model): the single seam. Mock + WebSocket feed both call this.
  // model = {
  //   session: { flag, track, lap, totalLaps, replay, elapsed, timeRemaining, timeTotal, lapsRemaining,
  //              fastestLap, type: "practice"|"qualify"|"warmup"|"race"|"testing"|null,
  //              typeLabel: "PRACTICE"|"" },
  //   focus:   { kind, onCamId, cars: [{ id, num, tla, full, first, last, pos, classPos,
  //              className, carTag, carModel, classColor, onCam, ...creds }],
  //              gap?, closing? } | null
  //            (gap/closing: live on-track interval (s) + trend (s/s, +ve=closing) for a
  //            battle; `closing` colours the gap green when the pair is coming together.
  //            first/last are the name split for the battle bar's two lines: both ""
  //            for an unnamed car, which sends the card back to the one-line `full`.)
  //            `kind` still picks the card TREATMENT (.leader, .trouble). There is no
  //            kind LABEL any more: it restated the picture and cost the best line.
  //   cars: [ { id, pos, num, tla, full, classId, className, carTag, classColor, classPos,
  //             interval, intervalLaps, toLeader, lapsDown, isLeader, classLeader,
  //             onPit, battle, fastest, lastLap, penalty, bestLap, lapGap, ...creds } ]
  //         Ordered as the tower shows them: the running order in a race (P1 first),
  //         by best lap in a non-race session (quickest first, no-time-yet at the
  //         bottom): see snapshot_to_model, which decides that once for both.
  // }
  //   lastLap = seconds for the car's last completed lap, or NULL if it has not set
  //   one. Never 0 and never iRacing's -1.0 sentinel: overlay.py drops those, and the
  //   outrigger renders nothing at all for a null (see outrigFor).
  //   fastest = this car holds the quickest lap of the session; at most one car in the
  //   list carries it, and NONE do until somebody has set a time (a long opening
  //   stretch of every session). session.fastestLap is that time, or null to match.
  //   penalty = "dsq" | "black" | "repair" (the meatball) | "warn" (furled black), or
  //   "" for the normal state. A decoded TOKEN, never iRacing's bitfield: overlay.py
  //   masks off `servicible`, which is not a flag and rides along on nearly every car
  //   of nearly every frame, and which is why "flags != 0" is not the test.
  //   Note this is NOT `flag`, that key is the driver's country emoji, in ...creds.
  //   bestLap / lapGap = the non-race classification: this car's own best lap, and how
  //   far off the quickest one it is (0.0 for the pace-setter). BOTH NULL in a race,
  //   which is what the gap column keys off, and null for a car with no lap yet.
  //   In a non-race session isLeader is false for every car: nobody leads a practice
  //   session, and interval / intervalLaps / lapsDown are all zeroed, since the row
  //   above is not a car this one is racing.
  // ...creds = { irating: "4.2k"|"" , licClass: "A"|"", licSR: "3.15"|"",
  //              licColor: 0xRRGGBB, country: "United Kingdom"|"", flag: "\u{1F1EC}\u{1F1E7}"|"" }
  //   flag is "" for the non-country flairs ("Global"): render the name instead.
  //   All are "" when the session has no real credentials (AI / unofficial), and every
  //   element that renders them hides itself when empty: never print a "0" iRating.
  //
  //   The TOWER ROW deliberately renders only a subset: position, number, TLA, gap and
  //   the outrigger. Class, licence and iRating still arrive in the payload and still
  //   render on the pop-in; they were cut from the row to keep the tower narrow.
  // ---- instant replay ----------------------------------------------------
  // Kinds come from ReplayDirector's candidates. The label is a fallback for a kind
  // this page has not been taught yet, so a new candidate type degrades to its own
  // words rather than to nothing.
  const RPY_WORD = { contact: "Contact", trouble: "In Trouble", lead_change: "For the Lead" };

  function replayCaption(r) {
    const what = RPY_WORD[r.kind] || r.label || "Replay";
    let who = "";
    if (r.car_number) {
      who = "#" + r.car_number + (r.driver ? " " + r.driver : "");
      if (r.position) who += " · P" + r.position;
    }
    if (r.other_number) {
      who += "  vs  #" + r.other_number + (r.other_driver ? " " + r.other_driver : "");
    }
    // Worth saying out loud: showing something the live camera never caught is the
    // entire justification for driving the sim's tape instead of an output buffer.
    if (r.missed_live) who += (who ? " · " : "") + "we missed it live";
    return { what, who };
  }

  // The wipe's covered window: the moment the frame is fully covered, and therefore
  // the moment the state underneath must flip. The animation holds a covered frame
  // from 42% to 58% of its 900ms, so 450 lands in the middle of a ~140ms window and
  // being a few ms out is invisible. Keep this in step with the @keyframes.
  const WIPE_COVER_MS = 450;
  const WIPE_TOTAL_MS = 900;
  // The way OUT waits for the banner to be gone this long. It rides a file the
  // director rewrites every frame of an excursion, and one frame without it (a read
  // that lost a race with the writer, Round 1 2026-09-20) must not wipe the replay off
  // and straight back on. The way IN is immediate: the wipe has to cover the seek.
  const RPY_OFF_MS = 250;
  let replayShowing = false;
  let rpyOffSince = null;
  let wipeBusy = false;

  function wipeTo(flip) {
    const v = els.wipe;
    let flipped = false;
    const doFlip = () => { if (!flipped) { flipped = true; flip(); } };
    // No element, or a wipe already running: flip bare. The decoration failing must
    // never leave the picture stuck in the wrong state.
    if (!v || wipeBusy) { doFlip(); return; }
    wipeBusy = true;
    // Restart the animation from the top even if it is mid-run: removing the class,
    // forcing a reflow and adding it back is the one reliable way to do that.
    v.classList.remove("is-playing");
    void v.offsetWidth;
    v.classList.add("is-playing");
    const finish = () => {
      v.classList.remove("is-playing");
      wipeBusy = false;
      doFlip();                      // idempotent: normally already flipped mid-wipe
    };
    setTimeout(doFlip, WIPE_COVER_MS);
    // Belt and braces: the animationend listener normally ends it, and this is what
    // stops a panel wedging on screen if the animation never fires at all.
    setTimeout(finish, WIPE_TOTAL_MS + 300);
    v.addEventListener("animationend", finish, { once: true });
  }

  function render(model) {
    const nowMs = performance.now();
    const rpy = model.instantReplay;
    const rpyOn = !!(rpy && rpy.active);
    // Caption first, so it is already correct when the wipe uncovers it.
    if (rpyOn) {
      const c = replayCaption(rpy);
      els.rpyWhat.textContent = c.what;
      els.rpyWho.textContent = c.who;
    }
    if (rpyOn) rpyOffSince = null;
    else if (replayShowing && rpyOffSince === null) rpyOffSince = nowMs;
    const rpyWant = rpyOn || (replayShowing && rpyOffSince !== null
                              && nowMs - rpyOffSince < RPY_OFF_MS);
    if (rpyWant !== replayShowing) {
      replayShowing = rpyWant;
      wipeTo(() => els.stage.classList.toggle("is-replay", rpyWant));
    }
    const gapTick = nowMs - gapTickAt >= GAP_TICK_MS;   // refresh the relatives at ~1 Hz
    if (gapTick) gapTickAt = nowMs;
    const s = model.session || {};
    const fc = FLAG_COLOR[s.flag] || "var(--flag-green)";
    els.flag.style.background = fc;
    els.flag.style.boxShadow = `0 0 14px 1px color-mix(in srgb, ${fc} 70%, transparent)`;
    if (s.track) els.track.textContent = s.track;
    // timed race -> lead with the countdown (endurance is run to the clock, not a lap
    // total); lap race -> lead with lap/total. The other value rides in the sub-line.
    let lapVal, lapCaption;
    if (s.timeRemaining != null) {
      lapVal = fmtClock(s.timeRemaining); lapCaption = "to go";
    } else if (s.totalLaps) {
      lapVal = `${s.lap}/${s.totalLaps}`; lapCaption = "lap";
    } else {
      lapVal = s.lap ? `L${s.lap}` : "-"; lapCaption = "lap";
    }
    // On the last lap the readout inverts: the words lead and the number becomes the
    // caption. A countdown reading 0:00 is not what the viewer needs told at that
    // moment, and the white bar on its own was not landing on stream (#65).
    els.lap.textContent = s.lastLap ? "LAST LAP" : lapVal;
    els.lapLabel.textContent = s.lastLap ? lapVal : lapCaption;
    els.cap.classList.toggle("is-last-lap", !!s.lastLap);
    els.session.innerHTML = sessionSub(s);
    // The gap column means something different in a session nobody is racing in.
    els.theadGap.textContent = (s.type && s.type !== "race") ? "Gap" : "Interval";
    // ...and in qualifying so does the outrigger: the best lap is the result there, where
    // a last lap is as likely to be an out lap. Qualifying ONLY: see outrigFor.
    const bestCol = s.type === "qualify";
    els.theadOutrig.textContent = bestCol ? "Best" : "Last";

    const cars = model.cars || [];

    // where is the camera car in the order? decides the scroll window + row highlight.
    const focusId = model.focus ? model.focus.onCamId : null;
    let focusRank = -1;
    for (let i = 0; i < cars.length; i++) if (cars[i].id === focusId) { focusRank = i; break; }

    // Size both boxes to the field, capped at the budgeted maxima: a 22-car grid fills
    // the tower, an 11-car practice session used to leave half of it as empty glass.
    // Shadowing the CSS caps on .tower keeps ONE copy of each of these numbers.
    const N = cars.length;
    const pinnedRows = Math.min(PINNED, N);
    const winRows = Math.min(WINDOW, Math.max(0, N - PINNED));
    els.tower.style.setProperty("--pinned", pinnedRows);
    els.tower.style.setProperty("--window", winRows);
    // On a field that fits inside the window this is PINNED, so paging below is a no-op
    // and the window can never move.
    const maxStart = Math.max(PINNED, N - winRows);
    const start = towerWindow(nowMs, focusRank, winRows, maxStart);

    const seen = new Set();
    cars.forEach((c, rank) => {
      seen.add(c.id);
      const r = ensureRow(c);
      const col = hex(c.classColor || 0x4c586a);

      // identity fields only change rarely, but they DO change: a team driver swap
      // renames a car mid-race on the 60s DriverInfo refresh, so the number and the
      // TLA are both in the key rather than assumed fixed for the session.
      const key = c.num + "|" + c.tla + "|" + col;
      if (r.key !== key) {
        r.num.textContent = "#" + c.num;
        r.tla.textContent = c.tla;
        r.el.style.setProperty("--cls", col);   // the row's class colour bar
        r.key = key;
      }
      // 0 = this session scores nobody (practice): show a dash, exactly as the pop-in
      // does, rather than numbering the rows and inventing a running order.
      r.pos.textContent = c.pos || "-";

      const pinned = rank < PINNED;

      // move to the right container (pinned block vs scroll window)
      const parent = pinned ? els.pinned : els.scrollInner;
      if (r.parent !== parent) { parent.appendChild(r.el); r.parent = parent; }

      // A scroll-region row is placed at its ABSOLUTE rank inside .scroll__inner, and the
      // container is translated below to bring the window's first row to the top. Paging
      // by rewriting every row's transform instead would fight the reorder animation:
      // seventeen rows would slide 420px while the incoming ones popped in from nowhere.
      // Every row stays in the DOM and .scroll clips the ones outside the window: rows
      // are hidden only once their car has left the field (see the sweep below), so the
      // only thing to undo here is a car that dropped out and came back.
      const y = pinned ? rank * ROW_H : (rank - PINNED) * ROW_H;
      r.el.style.transform = `translateY(${y}px)`;
      if (r.el.style.display) r.el.style.display = "";

      r.el.classList.toggle("alt", c.pos % 2 === 0);
      r.el.classList.toggle("leader", !!c.isLeader);
      r.el.classList.toggle("battle", !!c.battle && !c.isLeader);
      r.el.classList.toggle("fast", !!c.fastest);
      r.el.classList.toggle("pit", !!c.onPit);
      r.el.classList.toggle("garage", !!c.garage);
      r.el.classList.toggle("tow", !!c.tow);
      r.el.classList.toggle("oncam", c.id === focusId);

      // settle the interval to ~1 Hz (but show it immediately on a row's first sight,
      // and whenever its tag kind changes: LEADER / PIT / +N L should not lag a second)
      const [text, cls] = gapFor(c, s);
      if (gapTick || r.gapText == null || cls !== r.gapCls) {
        r.gap.textContent = text;
        r.gap.className = "gap" + (cls ? " " + cls : "");
        r.gapText = text;
        r.gapCls = cls;
      }

      // The outrigger is NOT throttled like the gap: a last lap changes once a lap and
      // PIT is a boolean, so neither can jitter, and holding PIT back by up to a second
      // is exactly the lag the gap throttle exists to avoid elsewhere.
      const [oText, oCls] = outrigFor(c, bestCol);
      if (oText !== r.outText || oCls !== r.outCls) {
        r.outrig.textContent = oText;
        r.outrig.className = "outrig" + (oCls ? " " + oCls : "");
        r.outText = oText;
        r.outCls = oCls;
      }
    });

    // drop rows for cars no longer in the field
    rows.forEach((r, id) => { if (!seen.has(id)) r.el.style.display = "none"; });

    // The scroll itself: ONE transform, on the container. Every row keeps its own for the
    // reorder animation, so the outrigger keeps riding its row and nothing moves twice.
    els.scrollInner.style.transform = `translateY(${-(start - PINNED) * ROW_H}px)`;

    fastestToast(cars, s.fastestLap);

    // "+N more" hints on the scroll window edges, and the edge fades that imply the same
    // thing. Both describe where the window IS (from `start`, the committed position),
    // never where it is heading.
    const hiddenTop = start - PINNED;
    const hiddenBot = N - (start + winRows);
    els.moreTop.textContent = hiddenTop > 0 ? `▲ P${PINNED + 1}: P${start}` : "";
    els.moreBot.textContent = hiddenBot > 0 ? `▼ +${hiddenBot} more` : "";
    els.scroll.classList.toggle("fits", hiddenTop <= 0 && hiddenBot <= 0);
    els.scroll.classList.toggle("empty", winRows === 0);

    renderPopins(model.focus);
  }

  // Driven from outside. The headless-Chrome harness (tests/test_overlay_js.py) loads
  // the page with ?harness and calls render() itself, so the page must start neither
  // the socket nor the mock. Exposed unconditionally: it costs nothing, and it lets a
  // model be pushed from the console of a live OBS browser source too.
  window.overlay = Object.freeze({ render });

  // --- data source: WebSocket if ?ws= is given; ?harness for neither; else the mock ---
  const wsUrl = params.get("ws");
  if (params.has("harness")) {
    els.session.textContent = "HARNESS";
  } else if (wsUrl) {
    els.session.textContent = "CONNECTING…";  // replaced by the badge/clock on first frame
    (function connect() {
      const ws = new WebSocket(wsUrl);
      ws.onmessage = (e) => {
        els.scroll.style.opacity = "";          // the feed is back: undo the dimming
        els.popins.style.opacity = "";
        try { render(JSON.parse(e.data)); } catch (_) { /* ignore */ }
      };
      ws.onerror = () => ws.close();
      ws.onclose = () => {
        // A dropped feed used to leave the last board on air, frozen, under a
        // pulsing LIVE badge, and the studio restarts the bridge routinely. Say so:
        // dim the timing, and replace the badge until the next frame replaces it back.
        els.scroll.style.opacity = "0.45";
        els.popins.style.opacity = "0.45";
        els.session.innerHTML = '<span class="badge badge--offline">RECONNECTING…</span>';
        setTimeout(connect, 1500);
      };
    })();
  } else {
    startMock();
  }

  // ---- built-in mock: a small multiclass field with a moving camera focus ----
  function startMock() {
    const PENALTY_DEMO = { 7: "repair", 14: "warn", 21: "black" };
    const CLASSES = [["GTP", 0xE6194B], ["LMP2", 0x3CB4E6], ["GTD", 0xF5B301]];
    const MODELS = ["Cadillac V-Series.R", "Oreca 07 Gibson", "Ferrari 296 GT3"];
    const COUNTS = [4, 6, 16];
    const NAMES = ["HALL", "MOSS", "REID", "KOVAC", "SATO", "ROSSI", "MULLER",
      "TANNER", "WEBB", "COSTA", "NORDIN", "IVANOV", "DE LUCA", "OKAFOR", "BJORN",
      "REYES", "PARK", "SILVA", "KELLER", "VOSS", "AMARI", "CRUZ", "FINN", "GARZA",
      "IRWIN", "JANSEN"];
    // Given names, so the preview shows the battle bar's real two-line treatment rather
    // than its fallback. The payload carries the split (overlay._name_parts) because a
    // surname is not simply the last word; the mock just has to supply both halves.
    const FIRSTS = ["Ryan", "Kimi", "Lewis", "Ana", "Yuki", "Marco", "Nils", "Beth",
      "Owen", "Tomas", "Elsa", "Pavel", "Gio", "Ade", "Lars", "Mateo", "Jin", "Rafa",
      "Katja", "Bo", "Zara", "Luis", "Erin", "Diego", "Neil", "Sanne"];
    // credentials, in iRacing's own licence colours (see _LIC_COLORS in overlay.py)
    const LICS = [["A", 0x0153DB], ["A", 0x0153DB], ["B", 0x00C702], ["C", 0xFEEC04]];
    const LANDS = [["United Kingdom", "\u{1F1EC}\u{1F1E7}"], ["United States", "\u{1F1FA}\u{1F1F8}"],
      ["Germany", "\u{1F1E9}\u{1F1EA}"], ["Italy", "\u{1F1EE}\u{1F1F9}"], ["Japan", "\u{1F1EF}\u{1F1F5}"],
      ["Brazil", "\u{1F1E7}\u{1F1F7}"], ["Netherlands", "\u{1F1F3}\u{1F1F1}"], ["Poland", "\u{1F1F5}\u{1F1F1}"],
      ["Spain", "\u{1F1EA}\u{1F1F8}"], ["Global", ""]];   // last one has no flag, on purpose
    const FIELD = [];
    let idx = 0;
    COUNTS.forEach((cnt, ci) => {
      for (let k = 0; k < cnt; k++) {
        const name = NAMES[idx % NAMES.length];
        const given = FIRSTS[idx % FIRSTS.length];
        const lic = LICS[idx % LICS.length];
        FIELD.push({
          id: idx, num: String((idx * 7 + 3) % 199 + 1),
          tla: name.replace(/ /g, "").slice(0, 3).toUpperCase(),
          full: given + " " + name[0] + name.slice(1).toLowerCase(),
          first: given, last: name[0] + name.slice(1).toLowerCase(),
          classId: ci, className: CLASSES[ci][0], classColor: CLASSES[ci][1],
          carModel: MODELS[ci], pace: 1 + ci * 0.05 + k * 0.004, grid: idx,
          irating: ((7800 - idx * 231) / 1000).toFixed(1) + "k",
          licClass: lic[0], licSR: (4.99 - (idx % 40) / 10).toFixed(2), licColor: lic[1],
          country: LANDS[idx % LANDS.length][0], flag: LANDS[idx % LANDS.length][1],
        });
        idx++;
      }
    });
    const N = FIELD.length;
    const schedule = [0, 1, 5, 6, 12, 20, 2, 9, 15, 3];
    const start = performance.now();
    let last = 0;

    function tick(now) {
      const t = (now - start) / 1000;
      FIELD.forEach((c) => {
        c.prog = t * (3 / c.pace) + Math.sin(t * 0.2 + c.grid) * 0.12 - c.grid * 0.05;
      });
      const order = [...FIELD].sort((a, b) => b.prog - a.prog);
      const leadProg = order[0].prog;
      const classLeader = {};
      order.forEach((c) => { if (!(c.classId in classLeader)) classLeader[c.classId] = c.id; });
      const pitId = 12, inPit = (Math.floor(t) % 30) < 6;

      // Session best, accumulated the way a real one is: each car's quickest lap so far,
      // never given back. The two cars with no lap at all can never hold it.
      //
      // `rub` is why this changes hands instead of being settled on the first tick. Pace
      // alone cannot do it: the class spread (0.4s between the GTP cars) is wider than
      // the lap-to-lap wobble (0.35s), so the quickest car's FIRST lap is already a time
      // nobody else can reach and the marker never moves. So the preview does what a real
      // session does: the track rubbers in, everyone's times come down, and the cars get
      // there at different moments. The purple walks the field early, the toast fires each
      // time it moves, and it settles onto the genuinely quickest car once `rub` is spent.
      FIELD.forEach((c) => {
        if (c.grid % 13 === 5) return;
        const rub = Math.max(0, 2.2 - t * 0.03 - (c.grid % 7) * 0.12);
        const lap = 68 + (c.pace - 1) * 100 + Math.sin(t * 0.3 + c.grid) * 0.35 + rub;
        if (c.best == null || lap < c.best) c.best = lap;
      });
      let fastestId = null, fastestLap = null;
      order.forEach((c) => {
        if (c.best != null && (fastestLap == null || c.best < fastestLap)) {
          fastestId = c.id; fastestLap = c.best;
        }
      });

      const cars = order.map((c, rank) => {
        const ahead = rank > 0 ? order[rank - 1] : null;
        const interval = ahead ? (ahead.prog - c.prog) * 1.4 : 0;
        const tl = (leadProg - c.prog) * 1.4;
        const aheadTl = ahead ? (leadProg - ahead.prog) * 1.4 : 0;
        return {
          id: c.id, pos: rank + 1, num: c.num, tla: c.tla, full: c.full,
          classId: c.classId, className: c.className, carTag: c.className, carModel: c.carModel,
          classColor: c.classColor,
          classPos: order.slice(0, rank + 1).filter((x) => x.classId === c.classId).length,
          interval: Math.max(0, interval),
          intervalLaps: Math.max(0, Math.floor(tl / 55) - Math.floor(aheadTl / 55)),
          toLeader: Math.max(0, tl), lapsDown: Math.max(0, Math.floor(tl / 55)),
          isLeader: rank === 0, classLeader: classLeader[c.classId] === c.id,
          onPit: c.id === pitId && inPit, battle: interval > 0 && interval < 0.55,
          fastest: c.id === fastestId,
          // One car under each flag, so the preview shows all three treatments at once.
          // Real ones are rare (a whole six-minute race capture held 115 car-frames of
          // meatball and 3 of a furled black), which is exactly why the design artifact
          // has to fake them or nobody ever sees what they look like.
          penalty: PENALTY_DEMO[c.id] || "",
          // Road Atlanta pace, spread by class. Two cars carry null on purpose: that is
          // the real "has not completed a lap" state (overlay.py filters iRacing's -1.0
          // sentinel to null), and the preview should show what the blank slot looks like.
          lastLap: c.grid % 13 === 5 ? null
            : 68 + (c.pace - 1) * 100 + Math.sin(t * 0.3 + c.grid) * 0.35,
          irating: c.irating, licClass: c.licClass, licSR: c.licSR, licColor: c.licColor,
          country: c.country, flag: c.flag,
        };
      });

      const fi = schedule[Math.floor(t / 5) % schedule.length];
      let fcar = cars.find((x) => x.pos === fi + 1) || cars[0];
      let kind, fcars;
      if ((t % 55) > 47 && (t % 55) < 53) {    // demo window: a car limping in the pack
        fcar = cars.find((x) => x.pos === 9) || fcar;
        kind = "trouble"; fcars = [fcar];
      } else {
        kind = fcar.isLeader ? "leader" : (fcar.battle ? "battle" : "follow");
        fcars = [fcar];
        if (fcar.battle) {
          const ahead = cars.find((x) => x.pos === fcar.pos - 1);
          if (ahead) fcars = [ahead, fcar];    // battle -> two pop-ins
        }
      }
      const focus = {
        kind, onCamId: fcar.id,
        // first/last come off FIELD, not off the row object: the real payload carries the
        // name split on the FOCUS cars only (overlay._focus_car), and the mock's rows
        // mirror the real row shape, which has no such fields.
        cars: fcars.map((c) => ({
          id: c.id, num: c.num, tla: c.tla, full: c.full,
          first: FIELD[c.id].first, last: FIELD[c.id].last,
          pos: c.pos, classPos: c.classPos,
          className: c.className, carTag: c.carTag, carModel: c.carModel,
          classColor: c.classColor, onCam: c.id === fcar.id,
          irating: c.irating, licClass: c.licClass, licSR: c.licSR, licColor: c.licColor,
          country: c.country, flag: c.flag,
        })),
      };
      if (kind === "battle" && fcars.length === 2) {
        focus.gap = Math.max(0.05, fcar.interval);   // interval to the car ahead in the pair
        focus.closing = Math.cos(t * 0.6) * 0.06;    // oscillate closing/easing for the preview
      }

      const flag = (t % 80 > 24 && t % 80 < 38) ? "yellow" : "green";
      render({
        session: {
          flag, track: "ROAD ATLANTA", lap: 12 + Math.floor(t / 30), totalLaps: null,
          replay: false, elapsed: t, timeRemaining: Math.max(0, 5400 - t),
          type: "race", typeLabel: "RACE", fastestLap,
        },
        focus, cars,
      });
    }
    function loop(now) { if (now - last > 80) { tick(now); last = now; } requestAnimationFrame(loop); }
    requestAnimationFrame(loop);
  }
})();
