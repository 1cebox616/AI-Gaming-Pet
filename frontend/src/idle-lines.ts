export const IDLE_LINES = [
  "今天也一起加油。",
  "我在这里陪着你。",
  "要不要活动一下肩膀？",
  "这一局慢慢来，稳住节奏就好。",
  "喝口水再继续，眼睛也该休息一下啦。",
  "刚才的操作很有想法，我已经记在小本本上了。",
  "如果觉得有点累，就把注意力放回下一步，不必急着赢下所有事情。",
  "屏幕前的你已经很努力了，先深呼吸一下，再带着一点点从容把接下来的挑战完成吧。",
] as const;

let nextIdleLineIndex = 0;

export function getNextIdleLine(): string {
  const line = IDLE_LINES[nextIdleLineIndex];
  nextIdleLineIndex = (nextIdleLineIndex + 1) % IDLE_LINES.length;
  return line;
}
