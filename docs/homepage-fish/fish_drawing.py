"""The homepage fish (frontend/assets/fish-swim.webp): the original drawing.

svg(t) returns one frame of the swim cycle as SVG, for a phase t in radians: the tail
narrows and sweeps, the pectoral fin paddles and the dorsal fin shifts. Drawn for this
project; no outside artwork. Standard library only. render_fish.py turns 12 frames
into the sprite.
"""

import math
W, H = 400, 200

def svg(t, k=1.0):
    s, c = math.sin(t), math.cos(t)
    tail_sx = 0.82 + 0.18 * c * k
    tail_sk = 6 * s * k
    pec = 14 * math.sin(t + 1.2) * k
    dor = 2.5 * s * k
    fin = 'fill="url(#fin)"'
    ray = 'stroke="#43514a" stroke-width=".55" fill="none" opacity=".35"'
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
<defs>
  <linearGradient id="body" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#2a3226"/><stop offset=".22" stop-color="#465039"/>
    <stop offset=".42" stop-color="#7c8772"/><stop offset=".6" stop-color="#c9cfc6"/>
    <stop offset=".85" stop-color="#e6e9e3"/><stop offset="1" stop-color="#b9c0b8"/>
  </linearGradient>
  <linearGradient id="len" x1="0" y1="0" x2="1" y2="0">
    <stop offset="0" stop-color="#000" stop-opacity=".18"/><stop offset=".25" stop-color="#000" stop-opacity="0"/>
    <stop offset=".8" stop-color="#000" stop-opacity="0"/><stop offset="1" stop-color="#000" stop-opacity=".12"/>
  </linearGradient>
  <linearGradient id="stripe" x1="0" y1="0" x2="1" y2="0">
    <stop offset="0" stop-color="#3fb7b0" stop-opacity="0"/>
    <stop offset=".1" stop-color="#3fc0b4" stop-opacity=".9"/><stop offset=".45" stop-color="#2b9fc0"/>
    <stop offset=".75" stop-color="#2f86b5" stop-opacity=".85"/><stop offset="1" stop-color="#2f86b5" stop-opacity="0"/>
  </linearGradient>
  <linearGradient id="fin" x1="0" y1="0" x2="1" y2="0">
    <stop offset="0" stop-color="#7f8f82" stop-opacity=".5"/><stop offset=".7" stop-color="#a9b6ab" stop-opacity=".2"/>
    <stop offset="1" stop-color="#a9b6ab" stop-opacity=".08"/>
  </linearGradient>
  <linearGradient id="shade" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#000" stop-opacity=".12"/><stop offset=".2" stop-color="#000" stop-opacity="0"/>
    <stop offset=".82" stop-color="#000" stop-opacity="0"/><stop offset="1" stop-color="#000" stop-opacity=".28"/>
  </linearGradient>
  <radialGradient id="sheen" cx=".3" cy=".35" r=".6">
    <stop offset="0" stop-color="#fff" stop-opacity=".35"/><stop offset="1" stop-color="#fff" stop-opacity="0"/>
  </radialGradient>
  <radialGradient id="iris" cx=".5" cy=".5" r=".5">
    <stop offset=".55" stop-color="#d6c99a"/><stop offset=".8" stop-color="#a99a66"/><stop offset="1" stop-color="#5d5636"/>
  </radialGradient>
  <linearGradient id="scalefade" x1="0" y1="0" x2="1" y2="0">
    <stop offset=".18" stop-color="#fff" stop-opacity="0"/><stop offset=".34" stop-color="#fff" stop-opacity="1"/>
    <stop offset=".85" stop-color="#fff" stop-opacity=".8"/><stop offset="1" stop-color="#fff" stop-opacity=".2"/>
  </linearGradient>
  <mask id="scalemask"><rect x="0" y="0" width="{W}" height="{H}" fill="url(#scalefade)"/></mask>
  <pattern id="scales" width="7" height="5.6" patternUnits="userSpaceOnUse" patternTransform="rotate(-4)">
    <path d="M0 5.6a3.5 3.5 0 0 1 7 0M-3.5 2.8a3.5 3.5 0 0 1 7 0M3.5 2.8a3.5 3.5 0 0 1 7 0" fill="none" stroke="#25301f" stroke-width=".45"/>
    <path d="M0.6 5.2a3 3 0 0 1 5.8 0" fill="none" stroke="#fff" stroke-width=".35" opacity=".5"/>
  </pattern>
  <filter id="grain" x="0" y="0" width="100%" height="100%">
    <feTurbulence type="fractalNoise" baseFrequency=".9" numOctaves="2" seed="7"/>
    <feColorMatrix values="0 0 0 0 .5  0 0 0 0 .5  0 0 0 0 .5  0 0 0 .5 0"/>
    <feComposite in2="SourceGraphic" operator="in"/>
  </filter>
  <filter id="b2" x="-10%" y="-60%" width="120%" height="220%"><feGaussianBlur stdDeviation="1.3"/></filter>
  <filter id="b1"><feGaussianBlur stdDeviation=".7"/></filter>
  <filter id="b4" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="3"/></filter>
  <clipPath id="clip"><path id="bp" d="M28 101C29 92 37 82 52 75C70 67 98 62 130 61C175 60 218 68 252 80C274 88 290 94 303 97.5L303 102.5C290 106 274 111 252 118C218 129 175 135 132 135C100 135 72 131 54 125C38 119 29 111 28 101Z"/></clipPath>
</defs>
<g transform="translate(300 100) skewY({tail_sk:.2f}) scale({tail_sx:.3f} 1) translate(-300 -100)">
  <path d="M297 94C318 85 344 70 372 60C367 77 356 91 348 100C356 109 367 123 372 141C344 131 318 116 297 106Z" {fin}/>
  <path d="M300 97L370 62M300 98L360 74M300 99L351 88M300 101L351 112M300 102L360 126M300 103L370 139" {ray}/>
  <path d="M372 60C367 77 356 91 348 100C356 109 367 123 372 141" stroke="#7d8a80" stroke-width=".5" fill="none" opacity=".35"/>
</g>
<g transform="rotate({dor:.2f} 188 63)">
  <path d="M168 63C174 50 183 39 195 34C199 44 204 55 210 65Z" {fin}/>
  <path d="M173 62L193 37M180 63L196 39M187 63L199 43M194 64L203 50" {ray}/>
</g>
<path d="M258 82C261 76 267 73 273 75C271 80 267 84 262 85Z" {fin} opacity=".9"/>
<path d="M190 132C205 143 232 147 262 138C253 129 238 124 222 123Z" {fin}/>
<path d="M198 133L211 143M210 131L223 145M223 129L236 144M237 127L250 140" {ray}/>
<use href="#bp" fill="url(#body)"/>
<g clip-path="url(#clip)">
  <rect x="0" y="0" width="{W}" height="{H}" fill="#fff" filter="url(#grain)" opacity=".18"/>
  <g mask="url(#scalemask)"><rect x="60" y="58" width="256" height="80" fill="url(#scales)" opacity=".38"/></g>
  <path d="M88 99C140 94 210 95 300 100.5L300 103C210 103 140 104 88 105Z" fill="#26302a" opacity=".25" filter="url(#b2)"/>
  <path d="M86 95C140 89.5 210 91 301 98.5L301 100.5C210 98 140 98.5 86 100.5Z" fill="url(#stripe)" filter="url(#b1)"/>
  <path d="M96 94.8C150 90.5 215 92 296 98.6" stroke="#c9fff6" stroke-width=".7" fill="none" opacity=".45"/>
  <rect x="0" y="0" width="{W}" height="{H}" fill="url(#shade)"/>
  <rect x="0" y="0" width="{W}" height="{H}" fill="url(#len)"/>
  <ellipse cx="120" cy="82" rx="95" ry="22" fill="url(#sheen)"/>
  <path d="M40 80C80 64 160 60 250 79" stroke="#fff" stroke-width="2" fill="none" opacity=".18" filter="url(#b1)"/>
  <path d="M93 69C102 84 103 112 93 128" stroke="#1f2a1e" stroke-width="2.4" fill="none" opacity=".16" filter="url(#b1)"/>
  <path d="M95 70C104 85 105 111 95 127" stroke="#fff" stroke-width=".7" fill="none" opacity=".3"/>
  <path d="M28 101C29 92 37 82 52 75C70 67 98 62 130 61C175 60 218 68 252 80C274 88 290 94 303 97.5L303 102.5C290 106 274 111 252 118C218 129 175 135 132 135C100 135 72 131 54 125C38 119 29 111 28 101Z" fill="none" stroke="#1d251c" stroke-width="3" opacity=".22" filter="url(#b4)"/>
</g>
<path d="M29 104.5C33 105.6 38 105.8 43 105" stroke="#2a3228" stroke-width=".8" fill="none" opacity=".45"/>
<ellipse cx="34.5" cy="96" rx="1" ry=".7" fill="#2a3228" opacity=".35"/>
<circle cx="60" cy="92" r="8.6" fill="url(#iris)"/>
<circle cx="60" cy="92" r="8.6" fill="none" stroke="#3a3a28" stroke-width=".6" opacity=".5"/>
<circle cx="59.6" cy="92.3" r="5.2" fill="#07090a"/>
<ellipse cx="57.6" cy="89.4" rx="2" ry="1.3" fill="#fff" opacity=".75" filter="url(#b1)"/>
<path d="M53 85C57 82.5 63 82.5 67 85" stroke="#fff" stroke-width=".6" fill="none" opacity=".35"/>
<g transform="rotate({pec:.2f} 97 109)">
  <path d="M97 106C112 107 128 114 139 125C123 124 106 119 96 113Z" {fin}/>
  <path d="M99 108L135 123M99 110L129 124M99 112L121 123" {ray}/>
</g>
<path d="M150 131C157 140 165 145 174 147C171 139 165 134 157 132Z" {fin}/>
</svg>'''
