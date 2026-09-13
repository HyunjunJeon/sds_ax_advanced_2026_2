import { expect, test } from "bun:test";

import { traverseSessionList } from "../src/sdk/session-list";

test("shared session-list traversal rejects after 10,000 continuation pages", async () => {
	let requests = 0;

	await expect(
		traverseSessionList(
			{},
			async () => {
				requests += 1;
				return { sessions: [], continuationCursor: `cursor-${requests}` };
			},
			response => response,
		),
	).rejects.toMatchObject({ kind: "page_budget_exceeded" });

	expect(requests).toBe(10_000);
});

test("shared session-list traversal seeds the seen cursor set from input", async () => {
	const cursors: string[] = [];

	await expect(
		traverseSessionList(
			{ cursor: "initial-cursor" },
			async input => {
				cursors.push(input.cursor);
				return { sessions: [], continuationCursor: "initial-cursor" };
			},
			response => response,
		),
	).rejects.toMatchObject({ kind: "repeated_cursor" });

	expect(cursors).toEqual(["initial-cursor"]);
});
