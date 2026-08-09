import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { getCurrentWebviewWindow } from "@tauri-apps/api/webviewWindow";

const PET_EXPRESSIONS = [
  "neutral",
  "happy",
  "angry",
  "surprised",
  "speechless",
] as const;

type PetExpression = (typeof PET_EXPRESSIONS)[number];

const PET_SVG = `
  <style>
    .pet-animation-layer {
      width: 172px;
      height: 172px;
      backface-visibility: hidden;
      pointer-events: none;
      transform: translateY(2px) scale(0.985);
      transform-origin: 50% 54%;
      will-change: transform;
    }

    .pet {
      width: 100%;
      height: 100%;
      overflow: visible;
      cursor: move;
      opacity: 1;
      pointer-events: none;
      transition: opacity 180ms ease;
      user-select: none;
    }

    .pet-animation-layer.is-dimmed .pet {
      opacity: 0.55;
    }

    #pet-breath {
      pointer-events: visiblePainted;
    }

    #pet-breath * {
      pointer-events: visiblePainted;
    }

  </style>
  <g id="pet-breath" data-pet-interactive>
    <path
      d="M54 151C37 136 35 104 48 79L47 48L76 64C91 57 109 57 124 64L153 48L152 79C165 104 163 136 146 151C126 169 74 169 54 151Z"
      fill="none"
      stroke="#ffffff"
      stroke-linejoin="round"
      stroke-width="11"
    />
    <path
      d="M54 151C37 136 35 104 48 79L47 48L76 64C91 57 109 57 124 64L153 48L152 79C165 104 163 136 146 151C126 169 74 169 54 151Z"
      fill="#79d9ef"
      stroke="#13233d"
      stroke-linejoin="round"
      stroke-width="4.5"
    />
    <path d="M55 66L73 76" fill="none" stroke="#f8fdff" stroke-linecap="round" stroke-width="5" />
    <path d="M145 66L127 76" fill="none" stroke="#f8fdff" stroke-linecap="round" stroke-width="5" />
    <ellipse cx="100" cy="142" rx="27" ry="10" fill="#4cc2df" opacity="0.7" />
    <g id="pet-face"></g>
  </g>
`;

const FACE_MARKUP: Record<PetExpression, string> = {
  neutral: `
    <ellipse cx="76" cy="101" rx="7" ry="9" fill="#13233d" />
    <ellipse cx="124" cy="101" rx="7" ry="9" fill="#13233d" />
    <path d="M83 124Q100 132 117 124" fill="none" stroke="#13233d" stroke-linecap="round" stroke-width="4.5" />
  `,
  happy: `
    <path d="M67 98Q76 109 85 98" fill="none" stroke="#13233d" stroke-linecap="round" stroke-width="5" />
    <path d="M115 98Q124 109 133 98" fill="none" stroke="#13233d" stroke-linecap="round" stroke-width="5" />
    <path d="M77 119Q100 145 123 119" fill="#f58ba3" stroke="#13233d" stroke-linecap="round" stroke-linejoin="round" stroke-width="4.5" />
  `,
  angry: `
    <path d="M66 106L85 96L82 108Z" fill="#13233d" />
    <path d="M134 106L115 96L118 108Z" fill="#13233d" />
    <path d="M79 132Q100 116 121 132" fill="none" stroke="#13233d" stroke-linecap="round" stroke-width="5" />
  `,
  surprised: `
    <ellipse cx="76" cy="100" rx="10" ry="13" fill="#13233d" />
    <ellipse cx="124" cy="100" rx="10" ry="13" fill="#13233d" />
    <ellipse cx="100" cy="128" rx="9" ry="12" fill="#f8fdff" stroke="#13233d" stroke-width="4.5" />
  `,
  speechless: `
    <path d="M66 102H86" fill="none" stroke="#13233d" stroke-linecap="round" stroke-width="6" />
    <path d="M114 102H134" fill="none" stroke="#13233d" stroke-linecap="round" stroke-width="6" />
    <path d="M80 128H120" fill="none" stroke="#13233d" stroke-linecap="round" stroke-width="5" />
  `,
};

const PET_BOUNDS_SAMPLE_INTERVAL_MS = 40;
const PET_BOUNDS_SAMPLE_DURATION_MS = 7_000;
const PET_INTERACTION_MARGIN_DIP = 2;
const PET_BREATH_UPDATE_INTERVAL_MS = 125;
const PET_BREATH_ONLINE_DURATION_MS = 3_500;
const PET_BREATH_OFFLINE_DURATION_MS = 7_000;
const PET_CONTEXT_MENU_COMMAND = "show_pet_menu";
const REPORT_PET_INTERACTION_REGION_COMMAND = "report_pet_interaction_region";
const MARK_PET_DRAGGING_COMMAND = "mark_pet_dragging";
const PET_WINDOW_VISIBILITY_EVENT = "pet-window-visibility";

const host = document.getElementById("app");

if (!(host instanceof HTMLDivElement)) {
  throw new Error("pet host is unavailable");
}

const pet = document.createElementNS("http://www.w3.org/2000/svg", "svg");
pet.classList.add("pet");
pet.setAttribute("aria-label", "AI Gaming Pet");
pet.setAttribute("role", "img");
pet.setAttribute("viewBox", "0 0 200 200");
pet.innerHTML = PET_SVG;

const petAnimationLayer = document.createElement("div");
petAnimationLayer.classList.add("pet-animation-layer");
petAnimationLayer.append(pet);
host.replaceChildren(petAnimationLayer);

const petBreath = pet.querySelector<SVGGElement>("#pet-breath");
const face = pet.querySelector<SVGGElement>("#pet-face");

if (petBreath === null) {
  throw new Error("pet breathing group is unavailable");
}

if (face === null) {
  throw new Error("pet face is unavailable");
}

const petDrawing: SVGGElement = petBreath;
const petFace: SVGGElement = face;

function isPaintedPetTarget(target: EventTarget | null): boolean {
  if (!(target instanceof SVGElement)) {
    return false;
  }

  const interactiveGroup = target.closest("g[data-pet-interactive]");
  return (
    interactiveGroup instanceof SVGGElement &&
    interactiveGroup.ownerSVGElement === pet
  );
}

interface InteractionBounds {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

function readPetDrawingBounds(): InteractionBounds {
  const drawingBounds = petDrawing.getBoundingClientRect();
  const windowBounds = document.documentElement.getBoundingClientRect();

  return {
    left: drawingBounds.left - windowBounds.left,
    top: drawingBounds.top - windowBounds.top,
    right: drawingBounds.right - windowBounds.left,
    bottom: drawingBounds.bottom - windowBounds.top,
  };
}

function unionBounds(
  current: InteractionBounds,
  sample: InteractionBounds,
): InteractionBounds {
  return {
    left: Math.min(current.left, sample.left),
    top: Math.min(current.top, sample.top),
    right: Math.max(current.right, sample.right),
    bottom: Math.max(current.bottom, sample.bottom),
  };
}

function waitForBoundsSample(): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, PET_BOUNDS_SAMPLE_INTERVAL_MS);
  });
}

async function sampleAndReportPetInteractionRegion(): Promise<void> {
  await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));

  const samplingStartedAt = performance.now();
  let sampledBounds = readPetDrawingBounds();

  while (
    performance.now() - samplingStartedAt <
    PET_BOUNDS_SAMPLE_DURATION_MS
  ) {
    await waitForBoundsSample();
    sampledBounds = unionBounds(sampledBounds, readPetDrawingBounds());
  }

  void invoke(REPORT_PET_INTERACTION_REGION_COMMAND, {
    x: sampledBounds.left - PET_INTERACTION_MARGIN_DIP,
    y: sampledBounds.top - PET_INTERACTION_MARGIN_DIP,
    width:
      sampledBounds.right - sampledBounds.left + PET_INTERACTION_MARGIN_DIP * 2,
    height:
      sampledBounds.bottom - sampledBounds.top + PET_INTERACTION_MARGIN_DIP * 2,
  }).catch((error: unknown) => {
    console.error("failed to report the pet interaction region", error);
  });
}

async function startPetDragging(): Promise<void> {
  try {
    await invoke(MARK_PET_DRAGGING_COMMAND);
    await getCurrentWebviewWindow().startDragging();
  } catch (error: unknown) {
    console.error("failed to start dragging the pet window", error);
  }
}

pet.addEventListener("mousedown", (event) => {
  if (event.button !== 0 || !isPaintedPetTarget(event.target)) {
    return;
  }

  event.preventDefault();
  void startPetDragging();
});

window.addEventListener(
  "contextmenu",
  (event) => {
    event.preventDefault();

    if (!isPaintedPetTarget(event.target)) {
      return;
    }

    void invoke(PET_CONTEXT_MENU_COMMAND, {
      position: { x: event.clientX, y: event.clientY },
    }).catch((error: unknown) => {
      console.error("failed to show the pet context menu", error);
    });
  },
  { capture: true },
);

let currentExpression: PetExpression = "neutral";
let isPetDimmed = false;
let breathPhase = 0;
let breathUpdatedAt = performance.now();
let breathTimer: number | undefined;

function easeInOut(progress: number): number {
  let lowerBound = 0;
  let upperBound = 1;

  for (let iteration = 0; iteration < 12; iteration += 1) {
    const parameter = (lowerBound + upperBound) / 2;
    const inverse = 1 - parameter;
    const x =
      3 * inverse * inverse * parameter * 0.42 +
      3 * inverse * parameter * parameter * 0.58 +
      parameter * parameter * parameter;

    if (x < progress) {
      lowerBound = parameter;
    } else {
      upperBound = parameter;
    }
  }

  const parameter = (lowerBound + upperBound) / 2;
  return (
    3 * (1 - parameter) * parameter * parameter +
    parameter * parameter * parameter
  );
}

function updatePetBreath(): void {
  const now = performance.now();
  const duration = isPetDimmed
    ? PET_BREATH_OFFLINE_DURATION_MS
    : PET_BREATH_ONLINE_DURATION_MS;
  breathPhase = (breathPhase + (now - breathUpdatedAt) / duration) % 1;
  breathUpdatedAt = now;

  const halfCycleProgress =
    breathPhase <= 0.5 ? breathPhase * 2 : (1 - breathPhase) * 2;
  const easedProgress = easeInOut(halfCycleProgress);
  const translateY = 2 - 5 * easedProgress;
  const scale = 0.985 + 0.03 * easedProgress;
  petAnimationLayer.style.transform = `translateY(${translateY}px) scale(${scale})`;
}

function startPetBreathing(): void {
  if (breathTimer !== undefined) {
    return;
  }

  breathUpdatedAt = performance.now();
  updatePetBreath();
  breathTimer = window.setInterval(
    updatePetBreath,
    PET_BREATH_UPDATE_INTERVAL_MS,
  );
}

function stopPetBreathing(): void {
  if (breathTimer === undefined) {
    return;
  }

  window.clearInterval(breathTimer);
  breathTimer = undefined;
}

function renderExpression(): void {
  pet.dataset.expression = currentExpression;
  petFace.innerHTML = FACE_MARKUP[currentExpression];
}

function nextExpression(): PetExpression {
  const currentIndex = PET_EXPRESSIONS.indexOf(currentExpression);
  return PET_EXPRESSIONS[(currentIndex + 1) % PET_EXPRESSIONS.length];
}

export function setPetExpression(expression?: PetExpression): void {
  currentExpression = expression ?? nextExpression();
  renderExpression();
}

export function isPetExpression(value: string): value is PetExpression {
  return PET_EXPRESSIONS.some((expression) => expression === value);
}

export function setPetDimmed(isDimmed: boolean): void {
  isPetDimmed = isDimmed;
  petAnimationLayer.classList.toggle("is-dimmed", isDimmed);
}

function setPetAnimationVisible(isVisible: boolean): void {
  if (!isVisible) {
    stopPetBreathing();
  } else {
    startPetBreathing();
  }
}

function updatePetAnimationVisibility(): void {
  setPetAnimationVisible(!document.hidden);
}

document.addEventListener("visibilitychange", updatePetAnimationVisibility);
void listen<boolean>(PET_WINDOW_VISIBILITY_EVENT, (event) => {
  setPetAnimationVisible(event.payload);
}).catch((error: unknown) => {
  console.error("failed to listen for pet window visibility changes", error);
});

renderExpression();
updatePetAnimationVisibility();
void sampleAndReportPetInteractionRegion();
