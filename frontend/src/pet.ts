const PET_EXPRESSIONS = ["neutral", "happy", "angry", "surprised", "speechless"] as const;

type PetExpression = (typeof PET_EXPRESSIONS)[number];

const PET_SVG = `
  <style>
    .pet {
      width: 172px;
      height: 172px;
      overflow: visible;
      cursor: move;
      opacity: 1;
      transition: opacity 180ms ease;
      user-select: none;
    }

    .pet.is-dimmed {
      opacity: 0.55;
    }

    #pet-breath {
      transform-box: view-box;
      transform-origin: 100px 108px;
      animation: pet-breathe 3.5s ease-in-out infinite;
    }

    .pet.is-dimmed #pet-breath {
      animation-duration: 7s;
    }

    @keyframes pet-breathe {
      0%,
      100% {
        transform: translateY(2px) scale(0.985);
      }

      50% {
        transform: translateY(-3px) scale(1.015);
      }
    }
  </style>
  <g id="pet-breath">
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
host.replaceChildren(pet);

const face = pet.querySelector<SVGGElement>("#pet-face");

if (face === null) {
  throw new Error("pet face is unavailable");
}

const petFace: SVGGElement = face;
let currentExpression: PetExpression = "neutral";

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

export function getPetExpression(): string {
  return currentExpression;
}

export function setPetDimmed(isDimmed: boolean): void {
  pet.classList.toggle("is-dimmed", isDimmed);
}

renderExpression();
