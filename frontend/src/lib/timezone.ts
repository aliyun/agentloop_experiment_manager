interface LocalDateTimeParts {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  second: number;
}

const PART_NAMES = ["year", "month", "day", "hour", "minute", "second"] as const;

function formatterFor(timezone: string): Intl.DateTimeFormat {
  const normalized = timezone.trim();
  if (!normalized) throw new Error("请填写 IANA 时区，例如 Asia/Shanghai。");
  try {
    return new Intl.DateTimeFormat("en-US-u-ca-gregory-nu-latn", {
      timeZone: normalized,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    });
  } catch {
    throw new Error(`IANA 时区“${normalized}”无效，请填写例如 Asia/Shanghai。`);
  }
}

function partsAt(instantMs: number, formatter: Intl.DateTimeFormat): LocalDateTimeParts {
  const values = new Map(
    formatter.formatToParts(new Date(instantMs))
      .filter((part) => PART_NAMES.includes(part.type as typeof PART_NAMES[number]))
      .map((part) => [part.type, Number(part.value)]),
  );
  return {
    year: values.get("year")!,
    month: values.get("month")!,
    day: values.get("day")!,
    hour: values.get("hour")!,
    minute: values.get("minute")!,
    second: values.get("second")!,
  };
}

function parseLocalDateTime(value: string): LocalDateTimeParts {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value.trim());
  if (!match) throw new Error("首次锚点格式无效，请重新选择日期和时间。");
  const [, yearText, monthText, dayText, hourText, minuteText, secondText = "00"] = match;
  const result = {
    year: Number(yearText),
    month: Number(monthText),
    day: Number(dayText),
    hour: Number(hourText),
    minute: Number(minuteText),
    second: Number(secondText),
  };
  const probe = new Date(Date.UTC(result.year, result.month - 1, result.day, result.hour, result.minute, result.second));
  if (
    result.year < 1000
    || result.year > 9999
    || probe.getUTCFullYear() !== result.year
    || probe.getUTCMonth() + 1 !== result.month
    || probe.getUTCDate() !== result.day
    || probe.getUTCHours() !== result.hour
    || probe.getUTCMinutes() !== result.minute
    || probe.getUTCSeconds() !== result.second
  ) {
    throw new Error("首次锚点不是有效的本地日期时间，请重新选择。");
  }
  return result;
}

function nominalUtc(parts: LocalDateTimeParts): number {
  return Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute, parts.second);
}

function sameLocalTime(left: LocalDateTimeParts, right: LocalDateTimeParts): boolean {
  return PART_NAMES.every((name) => left[name] === right[name]);
}

function offsetAt(instantMs: number, formatter: Intl.DateTimeFormat): number {
  const local = partsAt(instantMs, formatter);
  return nominalUtc(local) - instantMs;
}

/** Validate and normalize an IANA timezone without using the browser's local timezone. */
export function normalizeIanaTimezone(timezone: string): string {
  const normalized = timezone.trim();
  formatterFor(normalized);
  return normalized;
}

/**
 * Interpret a datetime-local value in the selected IANA timezone and return one UTC instant.
 * Nonexistent and repeated wall-clock times are rejected because the input contains no offset
 * with which a user could disambiguate them.
 */
export function zonedLocalDateTimeToUtc(value: string, timezone: string): string {
  const normalizedTimezone = normalizeIanaTimezone(timezone);
  const formatter = formatterFor(normalizedTimezone);
  const requested = parseLocalDateTime(value);
  const nominal = nominalUtc(requested);
  const possibleOffsets = new Set<number>();

  // Sampling both sides of the local date captures every offset involved in ordinary and DST
  // transitions without ever consulting the browser's own timezone.
  for (let hours = -72; hours <= 72; hours += 6) {
    possibleOffsets.add(offsetAt(nominal + hours * 60 * 60 * 1000, formatter));
  }

  const matches = [...possibleOffsets]
    .map((offset) => nominal - offset)
    .filter((candidate, index, candidates) => candidates.indexOf(candidate) === index)
    .filter((candidate) => sameLocalTime(partsAt(candidate, formatter), requested))
    .sort((left, right) => left - right);

  const displayValue = value.trim().replace("T", " ");
  if (matches.length === 0) {
    throw new Error(`首次锚点 ${displayValue} 在 ${normalizedTimezone} 不存在，可能位于夏令时跳时区间，请选择其它时间。`);
  }
  if (matches.length > 1) {
    throw new Error(`首次锚点 ${displayValue} 在 ${normalizedTimezone} 存在歧义（夏令时回拨后出现两次），请选择其它时间。`);
  }
  return new Date(matches[0]).toISOString();
}

/** Format an existing UTC instant for a datetime-local control in a named IANA timezone. */
export function utcInstantToZonedLocalInput(value: string | Date, timezone: string): string {
  const formatter = formatterFor(normalizeIanaTimezone(timezone));
  const instant = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(instant.getTime())) throw new Error("锚点不是有效的 UTC 时间。");
  const parts = partsAt(instant.getTime(), formatter);
  const two = (number: number) => String(number).padStart(2, "0");
  return `${String(parts.year).padStart(4, "0")}-${two(parts.month)}-${two(parts.day)}T${two(parts.hour)}:${two(parts.minute)}`;
}
