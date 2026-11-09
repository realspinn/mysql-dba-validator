import { BrandMark, Icon, type ICONS } from '../shared/Icon';
import { prefersDark, THEME_CHOICES, type ThemeChoice } from '../shared/theme';

export type View = 'dashboard' | 'review' | 'performance' | 'locks';

export const NAV: { id: View; label: string; icon: keyof typeof ICONS }[] = [
	{ id: 'dashboard', label: 'Dashboard', icon: 'dashboard' },
	{ id: 'review', label: 'SQL review', icon: 'review' },
	{ id: 'performance', label: 'Observed performance', icon: 'performance' },
	{ id: 'locks', label: 'Lock diagnostics', icon: 'locks' },
];

interface Props {
	views: View[];
	view: View;
	onView: (view: View) => void;
	onNewReview: () => void;
	context: string;
	contextOn: boolean;
	theme: ThemeChoice;
	onTheme: (choice: ThemeChoice) => void;
	ambient: boolean;
	onAmbient: () => void;
}

export function Sidebar({ views, view, onView, onNewReview, context, contextOn, theme, onTheme, ambient, onAmbient }: Props) {
	return (
		<aside className="sidebar" aria-label="Application">
			<div className="brand">
				<BrandMark />
				<div><strong>MySQL DBA Validator</strong><span>Pre-execution SQL review</span></div>
			</div>
			<nav aria-label="Views">
				{NAV.filter(item => views.includes(item.id)).map(item => (
					<button key={item.id} type="button" className="nav-item" aria-current={view === item.id ? 'page' : undefined}
						onClick={() => onView(item.id)}>
						<Icon name={item.icon} />{item.label}
					</button>
				))}
			</nav>
			<button type="button" className="btn btn-primary new-review" onClick={onNewReview}><Icon name="plus" />New SQL review</button>
			<div className="sidebar-foot">
				<div className="context-chip" aria-live="polite">
					<span className={'dot' + (contextOn ? ' on' : '')} aria-hidden="true" />
					<div><span className="eyebrow">Context</span><strong>{context}</strong></div>
				</div>
				<div className="segmented" role="radiogroup" aria-label="Theme">
					{THEME_CHOICES.map(choice => (
						<button key={choice} type="button" role="radio" aria-checked={theme === choice} onClick={() => onTheme(choice)}>
							{choice === 'system' ? `System (${prefersDark() ? 'dark' : 'light'})` : choice[0]!.toUpperCase() + choice.slice(1)}
						</button>
					))}
				</div>
				<button type="button" className="btn btn-quiet btn-sm ambient-toggle" aria-pressed={!ambient} onClick={onAmbient}>
					<Icon name={ambient ? 'pause' : 'play'} />
					{ambient ? 'Pause background motion' : 'Resume background motion'}
				</button>
			</div>
		</aside>
	);
}
