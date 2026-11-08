// The atmospheric top light: three soft glows (styles/ambient.css). Decorative only.
export function AmbientLight() {
	return (
		<div className="ambient" aria-hidden="true">
			<span className="glow glow-a" /><span className="glow glow-b" /><span className="glow glow-c" />
		</div>
	);
}
