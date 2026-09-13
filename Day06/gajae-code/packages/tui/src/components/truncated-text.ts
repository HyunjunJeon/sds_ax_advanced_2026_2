import type { Component } from "../tui";
import { padding, truncateToWidth } from "../utils";

/**
 * Text component that truncates to fit viewport width
 */
export class TruncatedText implements Component {
	#text: string;
	#paddingX: number;
	#paddingY: number;

	constructor(text: string, paddingX: number = 0, paddingY: number = 0) {
		this.#text = text;
		this.#paddingX = paddingX;
		this.#paddingY = paddingY;
	}

	invalidate(): void {
		// No cached state to invalidate currently
	}

	render(width: number): string[] {
		const result: string[] = [];
		const safeWidth = Math.max(0, Math.trunc(width));

		// Empty line padded to width
		const emptyLine = padding(safeWidth);

		// Add vertical padding above
		for (let i = 0; i < this.#paddingY; i++) {
			result.push(emptyLine);
		}

		// Calculate available width after horizontal padding
		const horizontalPadding = Math.max(
			0,
			Math.min(Math.trunc(this.#paddingX), Math.floor(Math.max(0, safeWidth - 1) / 2)),
		);
		const availableWidth = safeWidth - horizontalPadding * 2;

		// Take only the first line (stop at newline)
		let singleLineText = this.#text;
		const newlineIndex = this.#text.indexOf("\n");
		if (newlineIndex !== -1) {
			singleLineText = this.#text.substring(0, newlineIndex);
		}

		// Truncate text if needed (accounting for ANSI codes)
		const displayText = truncateToWidth(singleLineText, availableWidth);

		// Add horizontal padding
		const leftPadding = padding(horizontalPadding);
		const rightPadding = padding(horizontalPadding);
		const lineWithPadding = leftPadding + displayText + rightPadding;

		// Don't pad to full width - avoids trailing spaces when copying
		result.push(lineWithPadding);

		// Add vertical padding below
		for (let i = 0; i < this.#paddingY; i++) {
			result.push(emptyLine);
		}

		return result;
	}
}
