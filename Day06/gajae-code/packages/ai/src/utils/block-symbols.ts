export const kCursorExecResolved = Symbol("provider.block.cursorExecResolved");

export type CursorExecResolvedCarrier = object & { [kCursorExecResolved]?: true };

export function isCursorExecResolved(block: CursorExecResolvedCarrier | null | undefined): boolean {
	return block?.[kCursorExecResolved] === true;
}

export function copyCursorExecResolved(target: CursorExecResolvedCarrier, source: CursorExecResolvedCarrier): void {
	if (source[kCursorExecResolved] === true) target[kCursorExecResolved] = true;
}
