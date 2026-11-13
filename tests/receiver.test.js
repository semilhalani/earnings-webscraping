// Tests for the Apps Script receiver (finviz_data_receiver.gs), run with
// Node, which is the same JavaScript engine (V8) that Apps Script uses.
//
// Why this exists: the live scraper does not decide what is written to the
// sheet; the Apps Script receiver does. These tests load that EXACT file and
// check its refresh rule against the same cases Python is tested with
// (tests/fixtures/refresh_cases.json), plus the flag lifecycle and a full
// write into a pretend sheet. Nothing here talks to Google.
//
// Run locally:  node --test tests/receiver.test.js

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// Load the .gs file into a sandbox. Its top-level functions (mergeRow_,
// upsertRows_, ...) become properties of `gs`. Google's own services
// (SpreadsheetApp, LockService, ...) don't exist here, which is fine: the
// functions tested below never call them.
const gs = {};
vm.createContext(gs);
vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "finviz_data_receiver.gs"), "utf8"), gs);

// Objects made inside the sandbox are a different "realm"; this turns them
// into plain objects so assert.deepEqual can compare them with ours.
const plain = (x) => JSON.parse(JSON.stringify(x));

const { cases } = JSON.parse(
  fs.readFileSync(path.join(__dirname, "fixtures", "refresh_cases.json"), "utf8"),
);

// --- 1. The shared cases ----------------------------------------------------

for (const c of cases) {
  test(`shared case: ${c.name}`, () => {
    const result = plain(gs.mergeRow_(c.existing, c.incoming, c.columns, c.open_flags));
    assert.deepEqual(result, c.expected);
  });
}

// --- 2. Dates: Sheets turns "2026-10-22" into a date value ------------------

test("a date cell equals the same date sent as text (no false change)", () => {
  const result = gs.mergeRow_(
    { report_date: new Date(2026, 9, 22) }, // month 9 = October in JavaScript
    { report_date: "2026-10-22" },
    ["report_date"],
    {},
  );
  assert.equal(result.changed, false);
});

test("a date cell vs a different date is a change", () => {
  const result = gs.mergeRow_(
    { report_date: new Date(2026, 9, 22) },
    { report_date: "2026-10-23" },
    ["report_date"],
    {},
  );
  assert.equal(result.changed, true);
});

// --- 3. Flag lifecycle in Raw_DataFlags --------------------------------------

const where = { sheet: "Raw_EPSHistory", ticker: "AAPL", quarter: "Q3 '26", column: "reported" };
const sighting = { ...where, flag: "KEPT_OLD_VALUE", value_before: 6.77, finviz_value: "" };

test("a new problem adds one flag row", () => {
  const out = plain(gs.applyFlagChanges_([], [sighting], [], "2026-11-01T00:00:00+00:00"));
  assert.equal(out.added, 1);
  assert.deepEqual(out.rows, [{
    ...sighting,
    first_seen: "2026-11-01T00:00:00+00:00",
    last_seen: "2026-11-01T00:00:00+00:00",
    times_seen: 1,
    days_open: 0,
  }]);
});

test("seeing it again keeps first_seen and value_before, and updates the rest", () => {
  const day1 = plain(gs.applyFlagChanges_([], [sighting], [], "2026-11-01T00:00:00+00:00")).rows;
  const again = { ...sighting, value_before: 999 }; // must NOT replace the original
  const out = plain(gs.applyFlagChanges_(day1, [again], [], "2026-11-07T12:00:00+00:00"));
  assert.equal(out.added, 0);
  assert.equal(out.rows.length, 1);
  assert.equal(out.rows[0].first_seen, "2026-11-01T00:00:00+00:00");
  assert.equal(out.rows[0].last_seen, "2026-11-07T12:00:00+00:00");
  assert.equal(out.rows[0].times_seen, 2);
  assert.equal(out.rows[0].days_open, 6.5);
  assert.equal(out.rows[0].value_before, 6.77);
});

test("a solved problem deletes its flag row", () => {
  const day1 = plain(gs.applyFlagChanges_([], [sighting], [], "2026-11-01T00:00:00+00:00")).rows;
  const out = plain(gs.applyFlagChanges_(day1, [], [where], "2026-11-02T00:00:00+00:00"));
  assert.equal(out.resolved, 1);
  assert.deepEqual(out.rows, []);
});

test("a flag that changes type starts over as a new problem", () => {
  const day1 = plain(gs.applyFlagChanges_([], [sighting], [], "2026-11-01T00:00:00+00:00")).rows;
  const zero = { ...where, flag: "CHANGED_TO_ZERO", value_before: 6.77, finviz_value: 0 };
  const out = plain(gs.applyFlagChanges_(day1, [zero], [], "2026-11-03T00:00:00+00:00"));
  assert.equal(out.rows.length, 1);
  assert.equal(out.rows[0].flag, "CHANGED_TO_ZERO");
  assert.equal(out.rows[0].first_seen, "2026-11-03T00:00:00+00:00");
  assert.equal(out.rows[0].times_seen, 1);
});

test("flags on other cells are never touched", () => {
  const other = { ...sighting, ticker: "MSFT" };
  const day1 = plain(gs.applyFlagChanges_([], [sighting, other], [], "2026-11-01T00:00:00+00:00")).rows;
  const out = plain(gs.applyFlagChanges_(day1, [], [where], "2026-11-02T00:00:00+00:00"));
  assert.deepEqual(out.rows.map((r) => r.ticker), ["MSFT"]);
});

// --- 4. A full write into a pretend sheet -----------------------------------

// The few Sheet methods upsertRows_ uses, backed by a plain 2D array.
function fakeSheet(name, rows) {
  const data = rows.map((r) => r.slice());
  const lastRow = () => {
    for (let r = data.length; r > 0; r--) {
      if (data[r - 1].some((v) => v !== "" && v !== null && v !== undefined)) return r;
    }
    return 0;
  };
  return {
    data,
    getName: () => name,
    getLastRow: lastRow,
    getLastColumn: () => Math.max(0, ...data.map((r) => r.length)),
    deleteRows: (start, n) => data.splice(start - 1, n),
    getRange(row, col, numRows = 1, numCols = 1) {
      return {
        getValues: () => Array.from({ length: numRows }, (_, i) =>
          Array.from({ length: numCols }, (_, j) => {
            const v = (data[row - 1 + i] || [])[col - 1 + j];
            return v === undefined ? "" : v;
          })),
        setValues(values) {
          values.forEach((vals, i) => {
            while (data.length < row + i) data.push([]);
            vals.forEach((v, j) => { data[row - 1 + i][col - 1 + j] = v; });
          });
        },
      };
    },
  };
}

const HEADERS = ["ticker", "quarter", "quarter_sort", "estimate", "reported", "last_updated"];
const aapl = (reported, stamp) => ({
  ticker: "AAPL", quarter: "Q3 '26", quarter_sort: 263, estimate: 6.5, reported, last_updated: stamp,
});

function write(sheet, flags, rows) {
  return plain(gs.upsertRows_(sheet, HEADERS, rows, ["ticker", "quarter"], null, "refresh", flags));
}

test("refresh mode: add, skip unchanged, update, flag, then resolve", () => {
  const sheet = fakeSheet("Raw_EPSHistory", [HEADERS]);
  const flags = fakeSheet("Raw_DataFlags", []);

  // Day 1: a brand-new row is added.
  assert.deepEqual(write(sheet, flags, [aapl(6.77, "day1")]),
    { appended: 1, updated: 0, flags_open: 0, flags_new: 0, flags_resolved: 0 });

  // Day 2: identical values, so nothing is written and last_updated stays.
  assert.equal(write(sheet, flags, [aapl(6.77, "day2")]).updated, 0);
  assert.equal(sheet.data[1][5], "day1");

  // Day 3: Finviz revised the value, so the row is updated.
  assert.equal(write(sheet, flags, [aapl(6.98, "day3")]).updated, 1);
  assert.equal(sheet.data[1][4], 6.98);
  assert.equal(sheet.data[1][5], "day3");

  // Day 4: Finviz shows blank. We keep 6.98 and open a flag.
  const day4 = write(sheet, flags, [aapl(null, "2026-11-04T00:00:00+00:00")]);
  assert.equal(day4.updated, 0);
  assert.equal(day4.flags_new, 1);
  assert.equal(sheet.data[1][4], 6.98);
  assert.equal(flags.getLastRow(), 2); // header + one flag
  const flagHeader = flags.data[0];
  assert.equal(flags.data[1][flagHeader.indexOf("flag")], "KEPT_OLD_VALUE");
  assert.equal(flags.data[1][flagHeader.indexOf("value_before")], 6.98);

  // Day 5: Finviz shows 6.98 again. The flag row is deleted.
  const day5 = write(sheet, flags, [aapl(6.98, "2026-11-05T00:00:00+00:00")]);
  assert.equal(day5.flags_resolved, 1);
  assert.equal(flags.getLastRow(), 1); // only the header is left
});

test("the old finalize mode still works (needed while the update rolls out)", () => {
  const sheet = fakeSheet("Raw_EPSHistory", [HEADERS]);
  gs.upsertRows_(sheet, HEADERS, [aapl(6.77, "day1")], ["ticker", "quarter"], "reported", "finalize", null);
  const r = plain(gs.upsertRows_(sheet, HEADERS, [aapl(6.98, "day2")], ["ticker", "quarter"], "reported", "finalize", null));
  assert.equal(r.updated, 0); // finalize mode: a filled "reported" freezes the row
  assert.equal(sheet.data[1][4], 6.77);
});
