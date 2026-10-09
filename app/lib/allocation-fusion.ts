import { PhysicsDomainError, solveRollingForce, type RollingForceInput } from "./rolling-physics";

/** Provisional local experiment limits; not plant process or safety limits. */
export const ALLOCATION_FUSION_POLICY = {
  version: "dat-anchored-allocation-experiment-v1",
  maximumF1StrainShareShift: 0.05,
  minimumForceRatio: 0.8,
  maximumForceRatio: 1.25,
} as const;

export type TensionTriple = [number, number, number];
export type StandPair = [number, number];
type Equipment = Omit<RollingForceInput,
  "entry" | "exit" | "width" | "backTension" | "frontTension" | "flowStressMpa" | "widthFactor">;

export type AllocationFusionInput = {
  entry: number;
  exit: number;
  width: number;
  equipment: Equipment;
  widthFactors: StandPair;
  reference: {
    middle: number;
    tensions: TensionTriple;
    dataForces: StandPair;
    flowStresses: StandPair;
  };
  candidate: { middle: number; tensions: TensionTriple };
};

export type AllocationFusionResult = {
  status: "evaluated";
  referenceMiddle: number;
  candidateMiddle: number;
  referenceF1StrainShare: number;
  candidateF1StrainShare: number;
  referencePhysics: StandPair;
  candidatePhysics: StandPair;
  referenceData: StandPair;
  ratios: StandPair;
  fusedForces: StandPair;
} | { status: "unsupported"; reasons: string[] };

/**
 * Fixed-state, local geometry/tension response anchored to a DAT reference.
 * No target force is an input. Each stand keeps the reference effective-stress
 * calibration throughout the comparison; a band switch cannot manufacture a
 * force change. Material corrections belong in that reference exactly once.
 * This experimental result carries neither an error interval nor an approval.
 */
export function computeAllocationFusion(input: AllocationFusionInput): AllocationFusionResult {
  const { entry, exit, width, reference, candidate } = input;
  if (reference.tensions.length !== 3 || candidate.tensions.length !== 3 ||
    reference.dataForces.length !== 2 || reference.flowStresses.length !== 2 || input.widthFactors.length !== 2) {
    return { status: "unsupported", reasons: ["invalid_stand_or_tension_count"] };
  }
  if (![entry, exit, width, reference.middle, candidate.middle,
    ...reference.tensions, ...candidate.tensions, ...reference.dataForces,
    ...reference.flowStresses, ...input.widthFactors].every(Number.isFinite) ||
    !(entry > reference.middle && reference.middle > exit && exit > 0 && width > 0) ||
    !(entry > candidate.middle && candidate.middle > exit) ||
    [...reference.tensions, ...candidate.tensions].some(value => value < 0) ||
    [...reference.dataForces, ...reference.flowStresses, ...input.widthFactors].some(value => value <= 0)) {
    return { status: "unsupported", reasons: ["invalid_reference_or_candidate"] };
  }
  const totalStrain = Math.log(entry / exit);
  const referenceShare = Math.log(entry / reference.middle) / totalStrain;
  const candidateShare = Math.log(entry / candidate.middle) / totalStrain;
  if (Math.abs(candidateShare - referenceShare) > ALLOCATION_FUSION_POLICY.maximumF1StrainShareShift + 1e-12) {
    return { status: "unsupported", reasons: ["outside_local_allocation_range"] };
  }
  try {
    const forcePair = (middle: number, tensions: TensionTriple): StandPair => {
      const gauges = [entry, middle, exit];
      return [0, 1].map(stand => solveRollingForce({
        ...input.equipment,
        entry: gauges[stand], exit: gauges[stand + 1], width,
        backTension: tensions[stand], frontTension: tensions[stand + 1],
        flowStressMpa: reference.flowStresses[stand],
        widthFactor: input.widthFactors[stand],
      }).forceT) as StandPair;
    };
    const referencePhysics = forcePair(reference.middle, reference.tensions);
    const candidatePhysics = forcePair(candidate.middle, candidate.tensions);
    const ratios = candidatePhysics.map((force, stand) => force / referencePhysics[stand]) as StandPair;
    if (ratios.some(ratio => !Number.isFinite(ratio) ||
      ratio < ALLOCATION_FUSION_POLICY.minimumForceRatio || ratio > ALLOCATION_FUSION_POLICY.maximumForceRatio)) {
      return { status: "unsupported", reasons: ["physical_ratio_outside_experiment_range"] };
    }
    const fusedForces = reference.dataForces.map((force, stand) => force * ratios[stand]) as StandPair;
    if (!fusedForces.every(Number.isFinite)) return { status: "unsupported", reasons: ["nonfinite_fused_force"] };
    return {
      status: "evaluated", referenceMiddle: reference.middle, candidateMiddle: candidate.middle,
      referenceF1StrainShare: referenceShare, candidateF1StrainShare: candidateShare,
      referencePhysics, candidatePhysics, referenceData: [...reference.dataForces], ratios, fusedForces,
    };
  } catch (error) {
    if (!(error instanceof PhysicsDomainError)) throw error;
    return { status: "unsupported", reasons: ["physics_invalid", error.message] };
  }
}
