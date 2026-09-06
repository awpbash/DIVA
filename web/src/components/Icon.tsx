/** Inline SVG icons — no icon library dependency.
 *
 * Keep this set small. Every icon used in the UI must earn its place — no
 * decorative or dead icons. Strokes only, currentColor, 24x24 viewBox.
 */

interface Props {
  size?: number;
}

const wrap = (children: JSX.Element, size = 16) => (
  <svg
    xmlns="http://www.w3.org/2000/svg"
    viewBox="0 0 24 24"
    width={size}
    height={size}
    fill="none"
    stroke="currentColor"
    strokeWidth={1.8}
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden
  >
    {children}
  </svg>
);

export const IconPlus       = ({ size }: Props) => wrap(<><line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" /></>, size);
export const IconMinus      = ({ size }: Props) => wrap(<line x1="5" y1="12" x2="19" y2="12" />, size);
export const IconFit        = ({ size }: Props) => wrap(<><polyline points="8 3 3 3 3 8" /><polyline points="16 3 21 3 21 8" /><polyline points="3 16 3 21 8 21" /><polyline points="21 16 21 21 16 21" /></>, size);
export const IconChat       = ({ size }: Props) => wrap(<path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.5z" />, size);
export const IconGraph      = ({ size }: Props) => wrap(<><circle cx="6" cy="6" r="2.4" /><circle cx="18" cy="6" r="2.4" /><circle cx="6" cy="18" r="2.4" /><circle cx="18" cy="18" r="2.4" /><line x1="8.4" y1="6" x2="15.6" y2="6" /><line x1="6" y1="8.4" x2="6" y2="15.6" /><line x1="8" y1="16" x2="16" y2="8" /></>, size);
export const IconSend       = ({ size }: Props) => wrap(<><line x1="12" y1="19" x2="12" y2="5" /><polyline points="5 12 12 5 19 12" /></>, size);
export const IconDocument   = ({ size }: Props) => wrap(<><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /><line x1="8" y1="13" x2="16" y2="13" /><line x1="8" y1="17" x2="13" y2="17" /></>, size);
export const IconLibrary    = ({ size }: Props) => wrap(<><path d="M4 19V5a2 2 0 0 1 2-2h11" /><path d="M4 19a2 2 0 0 0 2 2h13V7H6a2 2 0 0 0-2 2v10z" /></>, size);
export const IconSparkle    = ({ size }: Props) => wrap(<path d="M12 3l1.7 4.6L18 9.3l-4.3 1.7L12 15l-1.7-4L6 9.3l4.3-1.7L12 3z" />, size);
export const IconCompass    = ({ size }: Props) => wrap(<><circle cx="12" cy="12" r="9" /><polygon points="16.2 7.8 13.4 13.4 7.8 16.2 10.6 10.6 16.2 7.8" /></>, size);
export const IconChevron    = ({ size }: Props) => wrap(<polyline points="6 9 12 15 18 9" />, size);
export const IconArrowLeft  = ({ size }: Props) => wrap(<><line x1="19" y1="12" x2="5" y2="12" /><polyline points="12 19 5 12 12 5" /></>, size);
export const IconArrowRight = ({ size }: Props) => wrap(<><line x1="5" y1="12" x2="19" y2="12" /><polyline points="12 5 19 12 12 19" /></>, size);
export const IconMic        = ({ size }: Props) => wrap(<><rect x="9" y="2" width="6" height="11" rx="3" /><path d="M5 11a7 7 0 0 0 14 0" /><line x1="12" y1="18" x2="12" y2="22" /></>, size);
export const IconClose      = ({ size }: Props) => wrap(<><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></>, size);
export const IconPanelRight = ({ size }: Props) => wrap(<><rect x="3" y="4" width="18" height="16" rx="2" /><line x1="15" y1="4" x2="15" y2="20" /></>, size);
export const IconTrash      = ({ size }: Props) => wrap(<><polyline points="3 6 5 6 21 6" /><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" /><path d="M10 11v6M14 11v6" /><path d="M9 6V4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2" /></>, size);
export const IconCheck      = ({ size }: Props) => wrap(<polyline points="20 6 9 17 4 12" />, size);
export const IconEye        = ({ size }: Props) => wrap(<><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z" /><circle cx="12" cy="12" r="3" /></>, size);
export const IconLayers     = ({ size }: Props) => wrap(<><polygon points="12 2 22 8.5 12 15 2 8.5 12 2" /><polyline points="2 15.5 12 22 22 15.5" /></>, size);

/** The DIVA mark: a small robot head, a reduction of the full logo (see
 * docs/images/logo.png) down to what still reads at sidebar/favicon size. */
export const IconBrand      = ({ size }: Props) => wrap(<><rect x="5" y="7" width="14" height="13" rx="5" /><line x1="12" y1="6.5" x2="12" y2="4.3" /><circle cx="12" cy="3.1" r="1" /><circle cx="9" cy="13.5" r="1.15" /><circle cx="15" cy="13.5" r="1.15" /></>, size);
