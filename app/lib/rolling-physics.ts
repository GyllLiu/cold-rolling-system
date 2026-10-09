/** Units: mm, MPa (= N/mm²), N; the external force unit T is tonne-force. */
export const NEWTONS_PER_TONNE_FORCE = 9806.65;

export class PhysicsDomainError extends Error {}

export function tensionStressMpa(tonnes: number, width: number, thickness: number) {
  if (![tonnes, width, thickness].every(Number.isFinite) || tonnes < 0 || width <= 0 || thickness <= 0) {
    throw new PhysicsDomainError("张力换算输入无效");
  }
  return NEWTONS_PER_TONNE_FORCE * tonnes / (width * thickness);
}

export function swiftMeanStress(parameters: { K_mpa: number; epsilon_0: number; n: number }, start: number, end: number) {
  if (![start, end, parameters.K_mpa, parameters.epsilon_0, parameters.n].every(Number.isFinite) ||
      start < 0 || end < start || parameters.K_mpa <= 0 || parameters.epsilon_0 <= 0 || parameters.n < 0) {
    throw new PhysicsDomainError("Swift应变或材料参数无效");
  }
  const base = parameters.epsilon_0 + start;
  const delta = end - start;
  if (delta === 0) return parameters.K_mpa * base ** parameters.n;
  const power = parameters.n + 1;
  // log1p/expm1 avoid subtracting nearly equal powers for light reductions.
  return parameters.K_mpa * base ** power * Math.expm1(power * Math.log1p(delta / base)) / (power * delta);
}

export function hillQ(mu: number, radius: number, entry: number, exit: number) {
  const reduction = (entry - exit) / entry;
  return 1.08 + 1.79 * mu * reduction * Math.sqrt(radius / entry) - 1.02 * reduction;
}

export type WidthBand = { width_low_mm: number; width_high_mm: number; factor: number };

/** C1 transition across each old band edge; constant tails, no new fit or overshoot. */
export function smoothWidthFactor(bands: WidthBand[], width: number, halfWidth = 10) {
  if (!Number.isFinite(width) || !bands.length || halfWidth <= 0) throw new PhysicsDomainError("宽度修正输入无效");
  for (let index = 0; index < bands.length - 1; index += 1) {
    const left = bands[index];
    const right = bands[index + 1];
    const edge = left.width_high_mm;
    const span = Math.min(halfWidth, (edge - left.width_low_mm) / 2, (right.width_high_mm - edge) / 2);
    if (width < edge - span) return left.factor;
    if (width <= edge + span) {
      const t = (width - edge + span) / (2 * span);
      return left.factor + t * t * (3 - 2 * t) * (right.factor - left.factor);
    }
  }
  return bands.at(-1)!.factor;
}

export type RollingForceInput = {
  entry: number; exit: number; width: number;
  backTension: number; frontTension: number; flowStressMpa: number;
  mu: number; radiusMm: number; youngModulusMpa: number; poissonRatio: number;
  backWeight: number; frontWeight: number; widthFactor: number;
  maximumIterations: number; radiusToleranceMm: number;
};

export function solveRollingForce(input: RollingForceInput) {
  const { entry, exit, width, flowStressMpa, radiusMm, youngModulusMpa, poissonRatio, widthFactor } = input;
  if (!Object.values(input).every(Number.isFinite) || !(entry > exit && exit > 0 && width > 0) ||
      flowStressMpa <= 0 || radiusMm <= 0 || youngModulusMpa <= 0 || poissonRatio < 0 || poissonRatio >= 0.5 ||
      input.mu < 0 || widthFactor <= 0 || input.maximumIterations < 1 || !Number.isInteger(input.maximumIterations) ||
      input.radiusToleranceMm <= 0 || input.backWeight < 0 || input.frontWeight < 0 ||
      Math.abs(input.backWeight + input.frontWeight - 1) > 1e-12) {
    throw new PhysicsDomainError("轧制力模型输入超出数学适用域");
  }
  const backTensionStressMpa = tensionStressMpa(input.backTension, width, entry);
  const frontTensionStressMpa = tensionStressMpa(input.frontTension, width, exit);
  const pressure = 1.15 * flowStressMpa - input.backWeight * backTensionStressMpa - input.frontWeight * frontTensionStressMpa;
  if (pressure <= 0) throw new PhysicsDomainError("张力超过当前变形抗力模型适用域，不能给出有效负荷");
  const draft = entry - exit;
  const compliance = 16 * (1 - poissonRatio ** 2) / (Math.PI * youngModulusMpa);
  let radius = radiusMm;
  for (let iteration = 1; iteration <= input.maximumIterations; iteration += 1) {
    const contactLengthMm = Math.sqrt(radius * draft);
    const qFactor = hillQ(input.mu, radius, entry, exit);
    const forceN = width * contactLengthMm * qFactor * pressure * widthFactor;
    const nextRadius = radiusMm * (1 + compliance * forceN / (width * draft));
    if (![forceN, nextRadius, qFactor].every(Number.isFinite) || qFactor <= 0 || forceN <= 0) {
      throw new PhysicsDomainError("轧辊压扁计算发散");
    }
    const residual = Math.abs(nextRadius - radius);
    if (residual <= input.radiusToleranceMm) {
      // All reported geometry and force belong to this same iterate.
      return {
        forceT: forceN / NEWTONS_PER_TONNE_FORCE,
        contactLengthMm, flattenedRadiusMm: radius, qFactor, flowStressMpa,
        widthCorrectionFactor: widthFactor, backTensionStressMpa, frontTensionStressMpa,
        converged: true as const, iterations: iteration, radiusResidualMm: residual,
      };
    }
    radius = nextRadius;
  }
  throw new PhysicsDomainError(`轧辊压扁在${input.maximumIterations}次迭代内未收敛`);
}
