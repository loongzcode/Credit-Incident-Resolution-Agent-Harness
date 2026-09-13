export const valueText = (value: string | number | boolean | null | undefined) => value == null ? 'UNKNOWN' : String(value);
export const timeText = (value: string | null | undefined) => value ? new Date(value).toISOString().replace('T', ' ').replace(/\.\d{3}Z$/, ' UTC') : '未知';
export const amountText = (minor: number, currency: string) => `${new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 }).format(minor / 100)} ${currency}`;
