const TYPEWRITER_INTERVAL_MS = 40;
const DISPLAY_DURATION_MS = 4_000;
const MAX_LINES = 3;
const MAX_CHARACTERS = 30;
const FADE_DURATION_MS = 180;

const BUBBLE_STYLE = `
  .speech-bubble {
    position: absolute;
    right: 118px;
    bottom: 151px;
    box-sizing: border-box;
    width: min(190px, calc(100% - 28px));
    padding: 10px 12px;
    color: #ffffff;
    background: rgb(15 23 42 / 86%);
    border: 1px solid rgb(255 255 255 / 72%);
    border-radius: 14px;
    box-shadow: 0 3px 9px rgb(0 0 0 / 35%);
    font-family: "Microsoft YaHei UI", sans-serif;
    font-size: 15px;
    font-weight: 600;
    line-height: 1.45;
    opacity: 0;
    pointer-events: none;
    transform: translateY(6px);
    transition:
      opacity ${FADE_DURATION_MS}ms ease,
      transform ${FADE_DURATION_MS}ms ease,
      visibility 0s linear ${FADE_DURATION_MS}ms;
    user-select: none;
    visibility: hidden;
  }

  .speech-bubble::before {
    position: absolute;
    right: 22px;
    bottom: -10px;
    width: 0;
    height: 0;
    border-top: 12px solid rgb(255 255 255 / 72%);
    border-right: 9px solid transparent;
    border-left: 9px solid transparent;
    content: "";
  }

  .speech-bubble::after {
    position: absolute;
    right: 22px;
    bottom: -9px;
    width: 0;
    height: 0;
    border-top: 10px solid rgb(15 23 42 / 86%);
    border-right: 8px solid transparent;
    border-left: 8px solid transparent;
    content: "";
  }

  .speech-bubble.is-visible {
    opacity: 1;
    transform: translateY(0);
    transition:
      opacity ${FADE_DURATION_MS}ms ease,
      transform ${FADE_DURATION_MS}ms ease,
      visibility 0s linear 0s;
    visibility: visible;
  }

  .speech-bubble__text {
    display: -webkit-box;
    max-height: ${MAX_LINES * 1.45}em;
    overflow: hidden;
    overflow-wrap: anywhere;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: ${MAX_LINES};
    word-break: break-word;
  }
`;

const host = document.getElementById("app");

if (!(host instanceof HTMLDivElement)) {
  throw new Error("speech bubble host is unavailable");
}

const style = document.createElement("style");
style.textContent = BUBBLE_STYLE;
document.head.append(style);

const bubble = document.createElement("div");
bubble.className = "speech-bubble";

const bubbleText = document.createElement("div");
bubbleText.className = "speech-bubble__text";
bubble.append(bubbleText);
host.append(bubble);

let typingTimer: number | undefined;
let hideTimer: number | undefined;
let messageRevision = 0;

function clearTimers(): void {
  if (typingTimer !== undefined) {
    window.clearTimeout(typingTimer);
    typingTimer = undefined;
  }

  if (hideTimer !== undefined) {
    window.clearTimeout(hideTimer);
    hideTimer = undefined;
  }
}

function limitMessage(message: string): string {
  const characters = Array.from(message.trim());

  if (characters.length <= MAX_CHARACTERS) {
    return characters.join("");
  }

  return `${characters.slice(0, MAX_CHARACTERS - 1).join("")}…`;
}

function hideCurrentMessage(revision: number): void {
  if (revision !== messageRevision) {
    return;
  }

  bubble.classList.remove("is-visible");
  hideTimer = undefined;
}

export function showSpeech(message: string): void {
  clearTimers();

  const revision = ++messageRevision;
  const characters = Array.from(limitMessage(message));
  bubbleText.textContent = "";
  bubble.classList.add("is-visible");

  const typeNextCharacter = (): void => {
    if (revision !== messageRevision) {
      return;
    }

    const nextCharacter = characters.shift();

    if (nextCharacter === undefined) {
      hideTimer = window.setTimeout(
        () => hideCurrentMessage(revision),
        DISPLAY_DURATION_MS,
      );
      typingTimer = undefined;
      return;
    }

    bubbleText.textContent += nextCharacter;
    typingTimer = window.setTimeout(typeNextCharacter, TYPEWRITER_INTERVAL_MS);
  };

  typeNextCharacter();
}

export function hideSpeech(): void {
  messageRevision += 1;
  clearTimers();
  bubble.classList.remove("is-visible");
}
