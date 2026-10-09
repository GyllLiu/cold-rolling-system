export type QuarterTurn = 0 | 90 | 180 | 270;
export const ROTATION_STORAGE_KEY = "capture-camera-rotation";

export function nextRotation(rotation: QuarterTurn): QuarterTurn {
  return ((rotation + 90) % 360) as QuarterTurn;
}

export function initialRotation(saved: string | null, width: number, height: number): QuarterTurn {
  if (saved !== null && ["0", "90", "180", "270"].includes(saved)) return Number(saved) as QuarterTurn;
  return width > height ? 90 : 0;
}

/** Fit the complete rotated frame; never use a crop to simulate portrait capture. */
export function frameGeometry(width: number, height: number, rotation: QuarterTurn, maxEdge = 2400) {
  const scale = Math.min(1, maxEdge / Math.max(width, height));
  const sourceWidth = Math.round(width * scale), sourceHeight = Math.round(height * scale);
  const sideways = rotation === 90 || rotation === 270;
  return { sourceWidth, sourceHeight,
    width: sideways ? sourceHeight : sourceWidth,
    height: sideways ? sourceWidth : sourceHeight,
    radians: rotation * Math.PI / 180 };
}
