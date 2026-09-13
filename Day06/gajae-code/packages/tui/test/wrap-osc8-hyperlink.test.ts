import { describe, expect, it } from "bun:test";
import { Text } from "../src/components/text";
import { visibleWidth, wrapTextWithAnsi } from "../src/utils";

// Issue #4711: when a URL hyperlink is soft-wrapped across visual rows, only
// the first row retained the OSC 8 open sequence — continuation rows rendered
// as plain non-clickable text. The wrap layer must carry the active OSC 8 open
// onto every continuation row and close each row so links never bleed into
// padding or adjacent text.

const OSC_OPEN = "\x1b]8;";
const OSC_CLOSE_BEL = "\x1b]8;;\x07";
const OSC_CLOSE_ST = "\x1b]8;;\x1b\\";
const URL = "https://example.com/a/very/long/url/that/wraps/over/two/lines/when/narrow";
const OPEN = (uri: string, st = false): string => `\x1b]8;;${uri}${st ? "\x1b\\" : "\x07"}`;

/** Strip OSC and CSI escapes, leaving only visible text. */
function plainText(line: string): string {
	return line.replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/gu, "").replace(/\x1b\[[0-?]*[ -/]*[@-~]/gu, "");
}

/** The exact open bytes a row must start its link span with. */
function linkOpens(line: string): string[] {
	return line.match(/\x1b\]8;[^\x07\x1b]*(?:\x07|\x1b\\)/gu) ?? [];
}

function urisIn(line: string): string[] {
	return linkOpens(line)
		.map(seq => seq.slice(5, seq.length - (seq.endsWith("\x07") ? 1 : 2)))
		.filter(uri => uri.length > 0);
}

describe("wrapTextWithAnsi OSC 8 hyperlink metadata (issue #4711)", () => {
	it("keeps the identical hyperlink open on every fragment of a 2-line wrap", () => {
		const rows = wrapTextWithAnsi(`docs ${OPEN(URL)}${URL}${OSC_CLOSE_BEL} trailing`, 20);

		expect(plainText(rows[0])).toBe("docs");
		expect(plainText(rows[rows.length - 1])).toBe("trailing");

		const fragments = rows.slice(1, rows.length - 1);
		expect(fragments.length).toBeGreaterThanOrEqual(2);
		for (const row of fragments) {
			// Visible fragment text is a contiguous slice of the URL...
			const text = plainText(row);
			expect(URL.includes(text)).toBe(true);
			// ...carries the identical open URI exactly once...
			expect(urisIn(row)).toEqual([URL]);
			// ...and self-closes so the link cannot bleed into row padding.
			expect(row.endsWith(OSC_CLOSE_BEL) || row.endsWith(OSC_CLOSE_ST)).toBe(true);
		}
		// Reassembling fragments reconstructs the URL exactly.
		expect(fragments.map(plainText).join("")).toBe(URL);
	});

	it("keeps hyperlink metadata on every fragment of a 3-line (and deeper) wrap", () => {
		for (const width of [12, 8, 6]) {
			const rows = wrapTextWithAnsi(`${OPEN(URL)}${URL}${OSC_CLOSE_BEL}`, width);
			expect(rows.length).toBeGreaterThanOrEqual(3);
			for (const row of rows) {
				expect(urisIn(row)).toEqual([URL]);
				expect(plainText(row)).not.toBe("");
			}
			expect(rows.map(plainText).join("")).toBe(URL);
			for (const row of rows) {
				expect(visibleWidth(row)).toBeLessThanOrEqual(width);
			}
		}
	});

	it("preserves link metadata across narrow/wide resize round-trips", () => {
		const source = `docs ${OPEN(URL)}${URL}${OSC_CLOSE_BEL} trailing`;
		const narrow = wrapTextWithAnsi(source, 20);
		const wide = wrapTextWithAnsi(source, 100);
		const narrowAgain = wrapTextWithAnsi(source, 20);

		// Wide layout fits on one row with a single open.
		expect(wide.length).toBe(1);
		expect(urisIn(wide[0])).toEqual([URL]);

		// Reflow is deterministic: the same width yields identical bytes after
		// detours through other widths (no stale spans from prior geometry).
		expect(narrowAgain).toEqual(narrow);

		// Every width in a resize sweep keeps the link on rows that contain URL
		// text and keeps it off pure plain-word rows. The plain-word fragments
		// ("docs", word-wrapped pieces of "trailing") are computed per width so
		// classification is exact rather than heuristic.
		for (const width of [6, 8, 10, 12, 15, 18, 20, 25, 30, 40, 50, 60, 79, 80, 90]) {
			const plainWordRows = new Set([
				...wrapTextWithAnsi("docs trailing", width).flatMap(row => plainText(row).trim().split(/\s+/u)),
				...wrapTextWithAnsi("docs trailing", width).map(row => plainText(row).trim()),
				"",
			]);
			for (const row of wrapTextWithAnsi(source, width)) {
				const text = plainText(row).trim();
				const uriList = urisIn(row);
				if (plainWordRows.has(text)) {
					// Pure plain-word row: no link metadata at all.
					expect(uriList).toEqual([]);
				} else {
					// Row contains URL text (possibly mixed with a plain word):
					// exactly one open carrying the identical target.
					expect(uriList).toEqual([URL]);
				}
			}
		}
	});

	it("keeps distinct targets for multiple URLs with mixed text and punctuation", () => {
		const first = "https://a.test/one/long/url/that/wraps/across/rows";
		const second = "https://b.test/two/long/url/that/wraps/across/rows";
		const rows = wrapTextWithAnsi(
			`see ${OPEN(first)}${first}${OSC_CLOSE_BEL}, and ${OPEN(second)}${second}${OSC_CLOSE_BEL}.`,
			20,
		);

		const seen: string[] = [];
		for (const row of rows) {
			for (const uri of urisIn(row)) {
				expect(uri === first || uri === second).toBe(true);
				seen.push(uri);
			}
		}
		expect(seen).toContain(first);
		expect(seen).toContain(second);

		// Punctuation and connective text stay unlinked: strip the known
		// connectives/punctuation from a linked row and require the remaining
		// letters to be a substring of that row's own URL target.
		for (const row of rows) {
			const text = plainText(row);
			const uriList = urisIn(row);
			if (uriList.length === 0) continue;
			const linked = uriList[0] === first ? first : second;
			const core = text
				.replace(/^(see|and)[\s,]*/u, "")
				.replace(/[\s,]*(and)$/u, "")
				.replace(/[.,]$/u, "")
				.trim();
			expect(core.length).toBeGreaterThan(0);
			expect(linked.includes(core)).toBe(true);
		}
		// Punctuation survives visibly outside every link open.
		const joined = rows.map(plainText).join("\n");
		expect(joined).toContain(",");
		expect(joined).toContain(".");
	});

	it("carries link metadata over wide Unicode before and inside the link vicinity", () => {
		const korean = "한글";
		const rows = wrapTextWithAnsi(`${korean} ${OPEN(URL)}${URL}${OSC_CLOSE_BEL}`, 20);
		expect(plainText(rows[0])).toBe(korean);
		expect(urisIn(rows[0])).toEqual([]);
		const fragments = rows.slice(1);
		for (const row of fragments) {
			expect(urisIn(row)).toEqual([URL]);
		}
		expect(fragments.map(plainText).join("")).toBe(URL);
		for (const row of rows) {
			expect(visibleWidth(row)).toBeLessThanOrEqual(20);
		}
	});

	it("composes hyperlink continuation with carried SGR styling", () => {
		const underlineOn = "\x1b[4m";
		const underlineOff = "\x1b[24m";
		const rows = wrapTextWithAnsi(`see ${underlineOn}${OPEN(URL)}${URL}${OSC_CLOSE_BEL}${underlineOff} now`, 20);

		expect(plainText(rows[0])).toBe("see");
		const fragments = rows.slice(1, rows.length - 1).filter(row => row.includes(OSC_OPEN));
		expect(fragments.length).toBeGreaterThanOrEqual(2);
		for (const row of fragments) {
			// Style restore and link open both precede the visible text.
			expect(row.startsWith(underlineOn)).toBe(true);
			expect(urisIn(row)).toEqual([URL]);
		}
	});

	it("does not extend a link over a hard newline onto adjacent text", () => {
		const rows = wrapTextWithAnsi(`${OPEN(URL)}${URL}${OSC_CLOSE_BEL}\nadjacent plain text after hard break`, 20);
		// Link rows all carry the open; the hard-newline lines carry none.
		const afterBreak = rows.slice(rows.findIndex(row => plainText(row).startsWith("adjacent")));
		expect(afterBreak.length).toBeGreaterThanOrEqual(1);
		for (const row of afterBreak) {
			expect(urisIn(row)).toEqual([]);
			expect(row.includes(OSC_OPEN)).toBe(false);
		}
		// Every pre-break row that shows URL text is linked.
		for (const row of rows.slice(0, rows.length - afterBreak.length)) {
			if (plainText(row) !== "") expect(urisIn(row)).toEqual([URL]);
		}
	});

	it("does not carry an unterminated link across a hard newline", () => {
		const rows = wrapTextWithAnsi(`${OPEN(URL)}short\nnext-line-here`, 40);
		expect(rows.length).toBe(2);
		expect(urisIn(rows[0])).toEqual([URL]);
		// The open was never closed in the source, but the hard break still
		// must not link the following logical line.
		expect(urisIn(rows[1])).toEqual([]);
		expect(rows[1].includes(OSC_OPEN)).toBe(false);
	});

	it("keeps an unterminated wrapped link clickable without leaking control bytes (#4711 red-team)", () => {
		// Regression: napi's JsString::into_utf16() delivers the input plus a
		// phantom trailing U+0000; when the wrap layer synthesizes a close for
		// an unterminated link after that phantom, the NUL became interior and
		// leaked into the rendered row / selection text.
		const rows = wrapTextWithAnsi(`${OPEN(URL)}${URL}`, 8);
		expect(rows.length).toBeGreaterThanOrEqual(3);
		for (const row of rows) {
			expect(row.includes("\u0000")).toBe(false);
			expect(urisIn(row)).toEqual([URL]);
			expect(plainText(row)).not.toBe("");
		}
		expect(rows.map(plainText).join("")).toBe(URL);
		// Copy/select surface stays the original URL text exactly.
		const copied = rows.map(plainText).join("");
		expect(copied).not.toMatch(/[\u0000-\u0008\u000b-\u001f\u007f]/u);
	});

	it("closes the link before the wrap boundary and leaves following rows unlinked", () => {
		// Close arrives BEFORE the final wrap break: the URL is linked on its
		// rows, and the words that wrap after the close never gain the link.
		const shortUrl = "https://example.com/long-ish";
		const rows = wrapTextWithAnsi(`${OPEN(shortUrl)}${shortUrl}${OSC_CLOSE_BEL} and then more words follow here`, 14);
		for (const row of rows) {
			const text = plainText(row);
			const isUrlRow =
				shortUrl.includes(text.replace(/\s*(and then more words follow here)$/u, "").trim()) &&
				/[a-z]/iu.test(text.replace(/^(and then more words follow here)\s*/u, ""));
			const uriList = urisIn(row);
			if (uriList.length > 0) {
				expect(uriList).toEqual([shortUrl]);
			} else {
				expect(isUrlRow).toBe(false);
			}
		}
		// Reassembled visible text matches a plain-text wrap of the same input
		// (wrap drops the space at break boundaries in both).
		const baseline = wrapTextWithAnsi(`${shortUrl} and then more words follow here`, 14).map(plainText).join("");
		const joined = rows.map(plainText).join("");
		expect(joined).toBe(baseline);
		expect(joined.startsWith(shortUrl)).toBe(true);
	});

	it("does not emit an escape-only continuation row after boundary whitespace (#4711 review P2)", () => {
		const shortUrl = "https://x.test/";
		// Trailing space after an unterminated link: the dropped whitespace
		// token leaves only the re-emitted state prefix; the final row must be
		// skipped rather than emitted as a blank clickable row.
		for (const width of [2, 3, 5]) {
			const rows = wrapTextWithAnsi(`${OPEN(shortUrl)}foo `, width);
			for (const row of rows) {
				expect(visibleWidth(row)).toBeGreaterThan(0);
			}
			const joined = rows.map(plainText).join("");
			expect(joined === "foo" || joined === "foo ").toBe(true);
			expect(urisIn(rows[0])).toEqual([shortUrl]);
		}
		// The same guarantee holds when the dropped whitespace precedes an
		// over-width word routed through the break-long-word path.
		for (const width of [2, 3]) {
			const rows = wrapTextWithAnsi(`${OPEN(shortUrl)}foo longword`, width);
			for (const row of rows) {
				expect(visibleWidth(row)).toBeGreaterThan(0);
			}
			for (const row of wrapTextWithAnsi("\x1b[31mfoo longword", width)) {
				expect(visibleWidth(row)).toBeGreaterThan(0);
			}
		}
		// SGR-styled input keeps the same guarantee (pre-existing blank row).
		for (const width of [2, 3, 5]) {
			for (const row of wrapTextWithAnsi("\x1b[31mfoo ", width)) {
				expect(visibleWidth(row)).toBeGreaterThan(0);
			}
		}
	});

	it("closes an unterminated link that fits on one row and trims before the close (#4711 review P2)", () => {
		const shortUrl = "https://x.test/";
		// Fits on one row: the fast path must still self-close so the link
		// cannot leak into caller-appended margins/padding.
		const fits = wrapTextWithAnsi(`${OPEN(shortUrl)}foo`, 40);
		expect(fits.length).toBe(1);
		expect(urisIn(fits[0])).toEqual([shortUrl]);
		expect(fits[0].endsWith(OSC_CLOSE_BEL)).toBe(true);

		// Already-terminated input is untouched; plain text stays plain.
		const closed = wrapTextWithAnsi(`${OPEN(shortUrl)}foo${OSC_CLOSE_BEL}`, 40);
		expect(closed.length).toBe(1);
		expect(plainText(closed[0])).toBe("foo");
		expect(wrapTextWithAnsi("foo bar", 40)).toEqual(["foo bar"]);

		// Trailing spaces on the final row of a wrapped unterminated link are
		// trimmed BEFORE the synthesized close (the BEL shields them otherwise).
		const wrapped = wrapTextWithAnsi(`${OPEN(shortUrl)}abcdefgh  `, 5);
		const last = wrapped[wrapped.length - 1];
		expect(plainText(wrapped.join(""))).toBe("abcdefgh");
		expect(last.includes("fgh  ")).toBe(false);
		expect(last.endsWith(OSC_CLOSE_BEL)).toBe(true);
	});

	it("applies a close attached to boundary whitespace before reopening the next row (#4711 review P1)", () => {
		// OPEN + "foo" + CLOSE + " bar" at width 3: the tokenizer attaches the
		// close to the whitespace token; that token is dropped at the wrap
		// boundary, so its transition must be applied before the continuation
		// prefix re-emits active state — otherwise `bar` renders linked to the
		// already-closed target.
		const shortUrl = "https://x.test/";
		const rows = wrapTextWithAnsi(`${OPEN(shortUrl)}foo${OSC_CLOSE_BEL} bar`, 3);
		expect(plainText(rows[0])).toBe("foo");
		expect(urisIn(rows[0])).toEqual([shortUrl]);
		for (const row of rows.slice(1)) {
			expect(urisIn(row)).toEqual([]);
			expect(row.includes(OSC_OPEN)).toBe(false);
		}
		// Multi-word continuation stays unlinked too.
		const multi = wrapTextWithAnsi(`${OPEN(shortUrl)}foo${OSC_CLOSE_BEL}  spaced words here`, 4);
		for (const row of multi.slice(1)) {
			expect(urisIn(row)).toEqual([]);
		}
	});

	it("keeps every fragment linked across an SGR reset inside the link", () => {
		const rows = wrapTextWithAnsi(`see \x1b[31m${OPEN(URL)}${URL}\x1b[0m${OSC_CLOSE_BEL} after`, 20);
		const linked = rows.filter(row => urisIn(row).length > 0);
		expect(linked.length).toBeGreaterThanOrEqual(2);
		for (const row of linked) {
			expect(urisIn(row)).toEqual([URL]);
		}
		const joined = rows.map(plainText).join("");
		expect(joined).toContain(URL);
	});

	it("re-emits ST-terminated open sequences verbatim", () => {
		const rows = wrapTextWithAnsi(`docs ${OPEN(URL, true)}${URL}${OSC_CLOSE_ST} trailing`, 20);
		const fragments = rows.slice(1, rows.length - 1);
		expect(fragments.length).toBeGreaterThanOrEqual(2);
		for (const row of fragments) {
			// The open bytes are the exact ST-terminated input sequence.
			expect(row.includes(OPEN(URL, true))).toBe(true);
			expect(row.endsWith(OSC_CLOSE_BEL) || row.endsWith(OSC_CLOSE_ST)).toBe(true);
		}
	});

	it("leaves plain wrapped text without hyperlinks untouched", () => {
		const rows = wrapTextWithAnsi("just some words that wrap across rows", 10);
		expect(rows.length).toBeGreaterThan(1);
		for (const row of rows) {
			expect(row.includes(OSC_OPEN)).toBe(false);
		}
	});
});

describe("Text component wrapped hyperlink rows (issue #4711)", () => {
	it("renders every wrapped URL fragment as part of the same link", () => {
		const component = new Text(`docs ${OPEN(URL)}${URL}${OSC_CLOSE_BEL} trailing`, 1, 0);
		const lines = component.render(22);
		const linked = lines.filter(line => line.includes(OSC_OPEN));
		expect(linked.length).toBeGreaterThanOrEqual(2);
		for (const line of linked) {
			expect(urisIn(line)).toEqual([URL]);
			// Link closes before the row is padded to width: padding spaces stay
			// outside the link.
			const closeIndex = Math.max(line.lastIndexOf(OSC_CLOSE_BEL), line.lastIndexOf(OSC_CLOSE_ST));
			const lastOpen = line.lastIndexOf(OPEN(URL));
			expect(closeIndex).toBeGreaterThan(lastOpen);
		}
		// Copy/select surface: stripping escapes and per-row padding
		// reconstructs the source words; URL fragments reassemble exactly.
		const reassembled = lines.map(line => plainText(line).trim()).join("");
		expect(reassembled.replace(/^docs/u, "").replace(/trailing$/u, "")).toBe(URL);
		expect(reassembled.startsWith("docs")).toBe(true);
		expect(reassembled.endsWith("trailing")).toBe(true);
	});

	it("emits no control bytes into the copied text", () => {
		const component = new Text(`${OPEN(URL)}${URL}${OSC_CLOSE_BEL}`, 1, 0);
		const lines = component.render(14);
		const copied = lines.map(line => plainText(line).trim()).join("");
		expect(copied).toBe(URL);
		// No residual ESC/BEL/ST control bytes leak into the selection text.
		expect(copied).not.toMatch(/[\u0000-\u0008\u000b-\u001f\u007f]/u);
	});
});
