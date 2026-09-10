/**
 * finviz_data_receiver.gs
 *
 * ALSO IN THIS VERSION: quarterSortKey_() (used only by the one-time
 * backfillQuarterSort() helper near the bottom) now recognizes
 * semi-annual (S1/S2, H1/H2) and annual (FY) reporting periods, not
 * just Q1-Q4 — mirrors the same fix made to normalize_quarter() /
 * quarter_sort_key() in finviz_earnings_scraper.py. Matters for any
 * ticker whose semi-annual row was already "finalized" (reported value
 * filled in) before that Python fix existed: normal upserts never
 * touch an already-finalized row again, so those rows' quarter_sort
 * would otherwise stay blank forever even after re-scraping. Re-run
 * backfillQuarterSort() once after deploying this version to patch
 * them. Also added 'Raw_GAAPEPSHistory' to backfillQuarterSort()'s
 * sheet list, since the Python script now scrapes a 4th widget — no
 * other change was needed for that new sheet since doPost() already
 * creates and writes any sheet name generically by NAME, not a
 * hardcoded list.
 *
 * IMPORTANT UPDATE — this version fixes a real data-corruption bug found
 * in live testing: adding a new column (quarter_sort) to the Python
 * script's schema, without clearing the sheet first, caused new rows to
 * write their values into the WRONG columns relative to old rows — e.g.
 * a revenue estimate landing in the "reported" column for some rows and
 * "estimate" for others, silently, with no error. Full explanation in
 * ensureHeaders_() below. THIS VERSION FIXES THAT: header columns are now
 * matched and extended BY NAME, so adding a new field to the Python
 * script in the future never misaligns existing data again.
 *
 * Also added in this version:
 * - In-place sorting (by ticker, then chronologically) after every
 *   write — a single bulk operation, not per-row inserts, so it's safe
 *   at scale.
 * - doGet(): lets the Python scraper fetch its ticker list FROM a sheet
 *   tab, instead of hardcoding tickers in a command or workflow file.
 *
 * SETUP (unchanged from before): paste into Extensions > Apps Script,
 * Deploy > New deployment > Web app > Execute as Me > Who has access
 * Anyone > Deploy. Use the resulting URL for both --web-app-url (writes)
 * and --tickers-from-sheet (reads).
 *
 * IF YOU'RE UPGRADING FROM THE PREVIOUS VERSION: your existing Raw_*
 * sheets may already have the column-misalignment bug from before this
 * fix existed. This version stops it from getting WORSE, but doesn't
 * retroactively repair rows already written wrong — see the chat
 * response for how to clean that up (clearing and re-scraping is
 * simplest for a test sheet with disposable data).
 */

function doPost(e) {
  // LockService: only ONE doPost execution runs the read-then-write
  // upsert logic at a time, script-wide, no matter how many callers hit
  // this Web App simultaneously. Without this, two near-simultaneous
  // calls could both read "this row doesn't exist yet" before either
  // has written, and both append it — duplicate rows, silently. waitLock
  // makes a second caller simply wait its turn (up to 30s) instead of
  // racing the first; releaseLock in `finally` guarantees the lock is
  // freed even if something above throws.
  var lock = LockService.getScriptLock();
  try {
    lock.waitLock(30000);
  } catch (lockErr) {
    return ContentService.createTextOutput(JSON.stringify({
      ok: false,
      error: 'Could not acquire lock within 30s — another write is likely stuck: ' + String(lockErr),
    })).setMimeType(ContentService.MimeType.JSON);
  }

  try {
    var payload = JSON.parse(e.postData.contents);

    // A dedicated "just sort now" action — self-contained inside the
    // Python script's own run, called once at the very end of run_batch()
    // (see trigger_sort_via_webapp() in finviz_earnings_scraper.py),
    // instead of depending on a separately-configured time-driven
    // trigger that lives outside version control and could be deleted
    // by accident with nothing to signal it's gone. Kept inside the
    // same lock as every other write, for the same reason: no sort
    // should ever run concurrently with an in-progress write.
    if (payload.action === 'sort_all') {
      sortAllDataSheets();
      return ContentService.createTextOutput(JSON.stringify({ ok: true, sorted: true }))
        .setMimeType(ContentService.MimeType.JSON);
    }

    var sheetName = payload.sheet;
    var headers = payload.headers;
    var keyColumns = payload.key_columns || ['ticker', 'quarter'];
    var finalizeColumn = payload.finalize_column || null;
    // mode: 'finalize' (default) is the existing earnings-data behavior
    // — an existing row is only updated if finalizeColumn is still
    // blank, and never touched again once it holds a real value.
    // 'overwrite' is new: an existing row (matched by keyColumns) is
    // ALWAYS replaced with the incoming data, no blank-check. Used only
    // for the ScraperProgress row, which needs to be updated every
    // checkpoint, not frozen after its first write.
    var mode = payload.mode || 'finalize';
    // clear_before_write: wipes all existing data rows (header kept)
    // before applying this write. Used only at the START of a fresh
    // day's --tickers-from-sheet sweep, to reset Raw_ScrapeIssues so it
    // reflects only TODAY's failures/no-data tickers, not an
    // ever-growing history mixing in issues that were already fixed
    // days ago. Runs inside the same lock as everything else here, so
    // it can't race with a concurrent write.
    var clearBeforeWrite = !!payload.clear_before_write;
    var newRows = payload.rows || [];

    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = ss.getSheetByName(sheetName);
    if (!sheet) {
      sheet = ss.insertSheet(sheetName);
    }

    if (clearBeforeWrite) {
      clearDataRows_(sheet);
    }

    var result = upsertRows_(sheet, headers, newRows, keyColumns, finalizeColumn, mode);
    // NOTE: sorting used to happen HERE, after every single write. Removed
    // deliberately — see sortAllDataSheets_() near the bottom of this file
    // for why and where sorting happens now. This was the real cause of a
    // production failure: re-sorting the WHOLE sheet on every checkpoint
    // (267 times per full 5,336-ticker sweep) got slower as the sheet grew
    // into thousands of rows, eventually exceeding even a 180s HTTP
    // timeout on a single write. Sort order has zero effect on
    // correctness — the upsert logic matches rows by key (ticker+quarter),
    // never by position — so there was no reason to pay that cost on
    // every single write.

    return ContentService.createTextOutput(JSON.stringify({
      ok: true,
      sheet: sheetName,
      appended: result.appended,
      updated: result.updated,
    })).setMimeType(ContentService.MimeType.JSON);

  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify({
      ok: false,
      error: String(err),
    })).setMimeType(ContentService.MimeType.JSON);
  } finally {
    lock.releaseLock();
  }
}

function doGet(e) {
  try {
    // mode=progress is new: returns the single ScraperProgress row as
    // JSON instead of a ticker list. mode=tickers (default, unchanged)
    // is the existing behavior used by --tickers-from-sheet.
    var mode = (e.parameter && e.parameter.mode) || 'tickers';
    var spreadsheetId = (e.parameter && e.parameter.spreadsheet_id) || null;
    var ss = spreadsheetId ? SpreadsheetApp.openById(spreadsheetId) : SpreadsheetApp.getActiveSpreadsheet();

    if (mode === 'progress') {
      var progressSheetName = (e.parameter && e.parameter.sheet) || 'ScraperProgress';
      var progressSheet = ss.getSheetByName(progressSheetName);
      if (!progressSheet || progressSheet.getLastRow() < 2) {
        // No sheet yet, or header-only — perfectly normal on the very
        // first run ever. Signal "no progress recorded" rather than an
        // error; the caller treats this as "start from the beginning."
        return jsonOutput_({ ok: true, found: false });
      }

      var pHeaders = progressSheet.getRange(1, 1, 1, progressSheet.getLastColumn()).getValues()[0];
      var pAllRows = progressSheet.getRange(2, 1, progressSheet.getLastRow() - 1, progressSheet.getLastColumn()).getValues();
      var rowIdIdx = pHeaders.indexOf('row_id');
      var lastUpdatedIdx = pHeaders.indexOf('last_updated');

      // Previously this just blindly read row 2, assuming exactly one row
      // always existed. That assumption broke silently in production: a
      // key-matching bug (see write_progress_via_webapp in the Python
      // script) meant every write appended a NEW row instead of updating
      // the existing one, so row 2 stayed frozen at the very first entry
      // ever written while dozens of newer rows piled up beneath it,
      // completely unread. That root cause is now fixed on the write
      // side, but this read is now also made robust independently of it:
      // find the row actually matching row_id="current" rather than
      // assuming position — and if for any reason several such rows
      // exist (e.g. legacy data from before this fix), take whichever
      // has the most recent last_updated, not just whichever comes first.
      var bestRow = null;
      var bestTimestamp = null;
      for (var r = 0; r < pAllRows.length; r++) {
        var row = pAllRows[r];
        if (rowIdIdx !== -1 && row[rowIdIdx] !== 'current') continue;
        var ts = lastUpdatedIdx !== -1 ? row[lastUpdatedIdx] : null;
        if (bestRow === null || (ts && (!bestTimestamp || new Date(ts) > new Date(bestTimestamp)))) {
          bestRow = row;
          bestTimestamp = ts;
        }
      }
      // Fallback for legacy rows written before row_id existed at all —
      // treat the sheet as empty of USABLE progress rather than reading
      // stale pre-fix data.
      if (bestRow === null) {
        return jsonOutput_({ ok: true, found: false });
      }

      var progressObj = {};
      pHeaders.forEach(function (h, i) { progressObj[h] = bestRow[i]; });
      return jsonOutput_({ ok: true, found: true, progress: progressObj });
    }

    var sheetName = (e.parameter && e.parameter.sheet) || 'Tickers';
    var column = (e.parameter && e.parameter.column) || 'ticker';

    // spreadsheet_id lets tickers be read from a DIFFERENT spreadsheet
    // than the one this Web App is deployed in — e.g. reading your real
    // Raw_Universe tab in your main "US Stocks using Claude" file, while
    // this Web App stays deployed in a separate sandbox spreadsheet for
    // writing earnings data. Requires the deploying account to have at
    // least view access to that other spreadsheet (works automatically
    // if you own both).
    var sheet = ss.getSheetByName(sheetName);
    if (!sheet) {
      return jsonOutput_({ ok: false, error: 'Sheet not found: ' + sheetName });
    }

    var data = sheet.getDataRange().getValues();
    var tickers = extractTickers_(data, column);
    if (tickers === null) {
      return jsonOutput_({
        ok: false,
        error: 'Column not found: ' + column + ' (available: ' + (data[0] || []).join(', ') + ')',
      });
    }

    return jsonOutput_({ ok: true, tickers: tickers });
  } catch (err) {
    return jsonOutput_({ ok: false, error: String(err) });
  }
}

function jsonOutput_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

/**
 * Pure function (no Apps Script APIs) so it's testable outside Apps
 * Script — given a 2D values array (row 0 = headers) and a column name,
 * returns a de-duplicated, uppercased, blank-filtered ticker list, or
 * null if the column isn't found.
 */
function extractTickers_(data, column) {
  if (!data || data.length === 0) return [];
  var headers = data[0];
  var colIdx = headers.indexOf(column);
  if (colIdx === -1) return null;

  var seen = {};
  var tickers = [];
  for (var i = 1; i < data.length; i++) {
    var v = data[i][colIdx];
    if (v === '' || v === null || v === undefined) continue;
    var t = String(v).trim().toUpperCase();
    if (t && !seen[t]) {
      seen[t] = true;
      tickers.push(t);
    }
  }
  return tickers;
}

/**
 * THE FIX for the corruption bug: ensures the sheet's header row
 * contains every name in `headers`, ADDING any missing ones as new
 * columns to the right rather than assuming the incoming payload's
 * column order matches the sheet's existing layout.
 *
 * What went wrong before: rowValues were built purely from the
 * PAYLOAD's header order and written starting at column A, with no
 * regard for what the sheet's row 1 actually said. When a new field
 * (quarter_sort) was added to the Python script without first clearing
 * the sheet, new rows shifted one column right of where old rows had
 * their data — same column, different meaning depending on the row.
 *
 * The fix: every row's values are now placed by NAME (via headerIndex),
 * against the FULL, current set of sheet columns — old and new alike.
 * A field missing from the header row is added as a new column; a field
 * missing from a given payload row for an EXISTING sheet column is left
 * alone (preserves other data), not blanked out.
 */
function ensureHeaders_(sheet, headers) {
  var lastRow = sheet.getLastRow();
  var lastCol = sheet.getLastColumn();

  if (lastRow === 0) {
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    var freshIndex = {};
    headers.forEach(function (h, i) { freshIndex[h] = i; });
    return { headerRow: headers.slice(), headerIndex: freshIndex };
  }

  var existingHeaders = sheet.getRange(1, 1, 1, lastCol).getValues()[0];
  var headerIndex = {};
  existingHeaders.forEach(function (h, i) { headerIndex[h] = i; });

  var missing = headers.filter(function (h) { return !(h in headerIndex); });
  if (missing.length > 0) {
    sheet.getRange(1, existingHeaders.length + 1, 1, missing.length).setValues([missing]);
    missing.forEach(function (h, i) { headerIndex[h] = existingHeaders.length + i; });
    existingHeaders = existingHeaders.concat(missing);
  }

  return { headerRow: existingHeaders, headerIndex: headerIndex };
}

function upsertRows_(sheet, headers, newRows, keyColumns, finalizeColumn, mode) {
  var schema = ensureHeaders_(sheet, headers);
  var headerIndex = schema.headerIndex;
  var numCols = schema.headerRow.length;

  var lastRow = sheet.getLastRow();
  var existingData = lastRow > 1 ? sheet.getRange(2, 1, lastRow - 1, numCols).getValues() : [];

  var keyIdx = keyColumns.map(function (k) { return headerIndex[k]; });

  var existingMap = {}; // "ticker|quarter" -> index into existingData
  existingData.forEach(function (row, i) {
    var key = keyIdx.map(function (idx) { return String(row[idx] || ''); }).join('|');
    existingMap[key] = i;
  });

  function buildRowByName(rowObj, existingRow) {
    var row = existingRow ? existingRow.slice() : new Array(numCols).fill('');
    headers.forEach(function (h) {
      var v = rowObj[h];
      row[headerIndex[h]] = (v === undefined || v === null) ? '' : v;
    });
    return row;
  }

  var rowsToAppend = [];
  var updates = []; // { rowNum (1-indexed sheet row), values }

  newRows.forEach(function (rowObj) {
    var key = keyColumns.map(function (k) { return String(rowObj[k] || ''); }).join('|');

    if (!(key in existingMap)) {
      rowsToAppend.push(buildRowByName(rowObj, null));
      return;
    }

    var i = existingMap[key];
    var existingRow = existingData[i];

    // mode 'overwrite': always replace the existing row, no blank-check
    // — used only for ScraperProgress, which must update every checkpoint.
    if (mode === 'overwrite') {
      updates.push({ rowNum: i + 2, values: buildRowByName(rowObj, existingRow) });
      return;
    }

    if (!finalizeColumn) return;

    var checkIdx = headerIndex[finalizeColumn];
    var existingVal = existingRow[checkIdx];

    if (existingVal === '' || existingVal === null || existingVal === undefined) {
      updates.push({ rowNum: i + 2, values: buildRowByName(rowObj, existingRow) });
    }
  });

  if (rowsToAppend.length > 0) {
    var startRow = sheet.getLastRow() + 1;
    sheet.getRange(startRow, 1, rowsToAppend.length, numCols).setValues(rowsToAppend);
  }
  updates.forEach(function (u) {
    sheet.getRange(u.rowNum, 1, 1, numCols).setValues([u.values]);
  });

  return { appended: rowsToAppend.length, updated: updates.length };
}

/**
 * Sorts all data rows (everything below row 1) by ticker, then by
 * quarter_sort if that column exists. One bulk range.sort() call — not
 * per-row inserts — so it stays fast even at thousands of rows.
 */
/**
 * Deletes all data rows (everything below the header), leaving row 1
 * intact. Used for clear_before_write. Deletes the rows outright rather
 * than just blanking their content, so getLastRow() correctly reports
 * back down to 1 afterward with no lingering empty-but-present rows.
 */
function clearDataRows_(sheet) {
  var lastRow = sheet.getLastRow();
  if (lastRow > 1) {
    sheet.deleteRows(2, lastRow - 1);
  }
}

function sortSheet_(sheet) {
  var lastRow = sheet.getLastRow();
  var lastCol = sheet.getLastColumn();
  if (lastRow < 3) return;

  var headerRow = sheet.getRange(1, 1, 1, lastCol).getValues()[0];
  var tickerIdx = headerRow.indexOf('ticker');
  var sortIdx = headerRow.indexOf('quarter_sort');

  var sortSpec = [];
  if (tickerIdx !== -1) sortSpec.push({ column: tickerIdx + 1, ascending: true });
  if (sortIdx !== -1) sortSpec.push({ column: sortIdx + 1, ascending: true });
  if (sortSpec.length === 0) return;

  sheet.getRange(2, 1, lastRow - 1, lastCol).sort(sortSpec);
}

/**
 * ONE-TIME MANUAL FIX — run this once, then you can ignore it forever.
 *
 * Backfills quarter_sort for rows that were already "finalized" (their
 * reported value was filled in) before this column existed. Our normal
 * upsert logic correctly never touches an already-reported row again —
 * actual reported earnings never change — but that same rule means those
 * rows never had the chance to pick up quarter_sort automatically once
 * it was added. This fixes ONLY the quarter_sort column for rows where
 * it's blank; every other value is left completely untouched.
 *
 * HOW TO RUN: in the Apps Script editor, use the function dropdown at
 * the top (next to Debug/Run) to select "backfillQuarterSort", then
 * click Run. Check View > Logs afterward to see how many rows were
 * fixed per sheet.
 */
function backfillQuarterSort() {
  var sheetNames = ['Raw_EPSHistory', 'Raw_GAAPEPSHistory', 'Raw_RevenueHistory', 'Raw_PostEarningsMoves'];
  var ss = SpreadsheetApp.getActiveSpreadsheet();

  sheetNames.forEach(function (name) {
    var sheet = ss.getSheetByName(name);
    if (!sheet) {
      Logger.log(name + ': sheet not found, skipping.');
      return;
    }

    var lastRow = sheet.getLastRow();
    var lastCol = sheet.getLastColumn();
    if (lastRow < 2) {
      Logger.log(name + ': no data rows, skipping.');
      return;
    }

    var headerRow = sheet.getRange(1, 1, 1, lastCol).getValues()[0];
    var quarterIdx = headerRow.indexOf('quarter');
    var sortIdx = headerRow.indexOf('quarter_sort');
    if (quarterIdx === -1 || sortIdx === -1) {
      Logger.log(name + ': missing "quarter" or "quarter_sort" column, skipping.');
      return;
    }

    var data = sheet.getRange(2, 1, lastRow - 1, lastCol).getValues();
    var filled = 0;

    for (var i = 0; i < data.length; i++) {
      var current = data[i][sortIdx];
      if (current !== '' && current !== null && current !== undefined) continue; // already has one

      var computed = quarterSortKey_(String(data[i][quarterIdx] || ''));
      if (computed !== null) {
        sheet.getRange(i + 2, sortIdx + 1).setValue(computed);
        filled++;
      }
    }

    Logger.log(name + ': backfilled ' + filled + ' of ' + data.length + ' rows.');
    sortSheet_(sheet);
  });

  Logger.log('Backfill complete — sheets have also been re-sorted.');
}

/**
 * Mirrors quarter_sort_key() / PERIOD_SORT_SLOT in finviz_earnings_scraper.py
 * — kept in sync manually since Apps Script and Python can't share a
 * module. Originally only understood "Q# 'YY"; extended to also handle
 * semi-annual (S1/S2, H1/H2) and annual (FY) reporters, using the SAME
 * slot numbers as the Python side (1-4 for Q, 5-6 for S/H, 9 for FY) so
 * quarter_sort values computed by either side are always identical for
 * the same label. If you ever change PERIOD_SORT_SLOT in the Python
 * script, mirror the change here too.
 */
function quarterSortKey_(quarter) {
  var m = quarter.match(/(Q[1-4]|S[1-2]|H[1-2]|FY) '(\d{2})/);
  if (!m) return null;
  var slots = { Q1: 1, Q2: 2, Q3: 3, Q4: 4, S1: 5, S2: 6, H1: 5, H2: 6, FY: 9 };
  var slot = slots[m[1]];
  if (slot === undefined) return null;
  var yy = parseInt(m[2], 10);
  return yy * 10 + slot;
}

/**
 * Sorts the four main data sheets (by ticker, then quarter_sort) — the
 * SAME sortSheet_() logic that used to run after every single write,
 * now run just once per full script run, instead of 267+ times per
 * full sweep.
 *
 * WHY THIS EXISTS: sort order is purely cosmetic — for a human scrolling
 * through the sheet to see it in a sensible order. It has ZERO effect on
 * correctness: the upsert logic in upsertRows_() matches existing rows
 * by key (ticker + quarter), never by row position, and any downstream
 * formula reading this data (e.g. a SORT() formula in another sheet)
 * already re-sorts on read regardless of the raw sheet's own order.
 * There was never a functional reason to pay a full-range sort's cost on
 * every single write — only a cosmetic one, and cosmetic doesn't need to
 * be instant.
 *
 * HOW THIS RUNS NOW (primary path, no setup needed): the Python script
 * calls this itself, once, right after its own last write of every run
 * — see trigger_sort_via_webapp() in finviz_earnings_scraper.py, POSTing
 * {"action": "sort_all"}. This is intentionally self-contained: sorting
 * is now tied to the script actually running, not to a separately
 * configured setting that lives outside version control and could be
 * silently deleted with nothing to signal it's gone.
 *
 * OPTIONAL EXTRA SAFETY NET: you can still also wire this up as a daily
 * time-driven trigger if you want a redundant backstop (harmless either
 * way — sorting an already-sorted sheet is cheap):
 *   1. In the Apps Script editor, click the clock icon ("Triggers") in
 *      the left sidebar.
 *   2. Click "+ Add Trigger" (bottom right).
 *   3. Function to run: sortAllDataSheets
 *   4. Event source: Time-driven
 *   5. Type of time based trigger: Day timer
 *   6. Time of day: pick any window unlikely to overlap an active
 *      scrape run.
 *   7. Save.
 * Not required for correctness — the Python-triggered call above already
 * covers every run on its own.
 */
function sortAllDataSheets() {
  var sheetNames = ['Raw_EPSHistory', 'Raw_GAAPEPSHistory', 'Raw_RevenueHistory', 'Raw_PostEarningsMoves'];
  var ss = SpreadsheetApp.getActiveSpreadsheet();

  sheetNames.forEach(function (name) {
    var sheet = ss.getSheetByName(name);
    if (!sheet) {
      Logger.log(name + ': sheet not found, skipping.');
      return;
    }
    sortSheet_(sheet);
    Logger.log(name + ': sorted.');
  });

  Logger.log('sortAllDataSheets complete.');
}