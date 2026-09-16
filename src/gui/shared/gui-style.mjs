// Shared browser/Host presentation contract. No DOM, arbitrary CSS, or business authority.
const palettes = {
  default: { light: ['#f5f6f8','#ffffff','#172033','#586174','#cbd2dd','#365acb','#ffffff'], dark: ['#111722','#1c2533','#eef2f8','#a8b3c4','#48566a','#9ab3ff','#111722'] },
  ocean: { light: ['#eff7fb','#ffffff','#102d3b','#496475','#bdd4e1','#00678a','#ffffff'], dark: ['#0c202c','#153342','#edf8ff','#a2c6d8','#3e6376','#77d5f7','#0c202c'] },
  forest: { light: ['#f1f7f2','#ffffff','#183526','#506d5b','#c2d5c7','#286443','#ffffff'], dark: ['#10251a','#1a3425','#eff9f2','#accbb5','#41694f','#8fdaad','#10251a'] },
  warm: { light: ['#fff7ed','#fffdf9','#40271b','#795f50','#e1cbbb','#964619','#ffffff'], dark: ['#2a1b15','#3b2920','#fff4e8','#d5b8a1','#795544','#ffba85','#2a1b15'] },
  mono: { light: ['#f4f4f4','#ffffff','#202020','#626262','#cecece','#353535','#ffffff'], dark: ['#171717','#262626','#f5f5f5','#b4b4b4','#555555','#dedede','#171717'] }
};
const colorKeys = ['background','surface','text','muted','border','primary','onPrimary'];
const fonts = {
  system: 'system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
  sans: 'Arial, "Helvetica Neue", sans-serif',
  serif: 'Georgia, "Times New Roman", serif',
  mono: 'ui-monospace, SFMono-Regular, Consolas, monospace'
};
const shadows = { none: 'none', soft: '0 2px 8px rgb(0 0 0 / 0.12)', medium: '0 6px 20px rgb(0 0 0 / 0.18)' };
function freeze(value) { if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value); } return value; }
export const GUI_STYLE_CONTRACT = freeze({
  version: 1,
  workflow: ['Read the current draft, revision, module catalog and style contract.', 'Choose a preset, then use semantic color pairs and bounded typography/spacing. Keep module instanceId and business context unchanged.', 'Check page-to-parent-to-child appearance inheritance in both light and dark modes. Use presets when custom pairs fail contrast.', 'Update against baseRevision, validate that exact revision, then preview for user review. Never claim an unvalidated design is applied.'],
  guidance: ['Prefer one primary color and consistent typography/radius/spacing.', 'Keep secondary text readable and preserve native warning, error, success and disabled states.', 'Check narrow layouts and avoid large padding that consumes module space. Core business controls remain native.', 'Page styles stay local; shell styles apply across the workspace. Removing styles restores inherited/default appearance.'],
  presets: Object.fromEntries(Object.entries(palettes).map(([name, modes]) => [name, Object.fromEntries(Object.entries(modes).map(([mode, values]) => [mode, Object.fromEntries(colorKeys.map((key, index) => [key, values[index]]))]))])),
  examples: { theme: { preset: 'ocean', mode: 'system', typography: { font: 'system', size: 14, lineHeight: 1.5 }, spacing: 16, radius: 12, shadow: 'soft' }, appearance: { surface: 'surface', padding: 16, radius: 12, border: true } },
  theme: {
    preset: Object.keys(palettes), mode: ['light','dark','system'], accent: '#RRGGBB', density: ['comfortable','compact'],
    colors: { modes: ['light','dark'], fields: colorKeys, format: '#RRGGBB', contrast: 'Explicit colors must produce text/background, text/surface and onPrimary/primary contrast >= 4.5 in both effective palettes. Legacy accent automatically picks black/white onPrimary unless explicitly supplied.' },
    typography: { font: Object.keys(fonts), size: [12,20], lineHeight: [1.2,2], headingScale: [1.1,1.8] },
    spacing: [0,32], radius: [0,24], borderWidth: [0,3], shadow: Object.keys(shadows)
  },
  appearance: { surface: ['transparent','base','surface','primary'], padding: [0,32], radius: [0,24], border: 'boolean', shadow: Object.keys(shadows), textTone: ['default','muted','primary'], fontSize: [12,40], fontWeight: [400,500,600,700], align: ['left','center','right'] },
  rules: 'Optional appearance on every page and layout node. Only named tokens and bounded numeric values; no arbitrary CSS, HTML, selectors, fonts, images, URLs or scripts. Effective inherited appearance text/background and text/surface must retain contrast >= 4.5 in both modes; primary surface with primary text is invalid. Styling never changes instance identity, data references or business permissions. Root style defaults apply only when a new style field is configured; legacy mode/accent/density remain compatible.',
  units: 'Sizes, spacing, radius and borderWidth are integer pixels. lineHeight and headingScale are unitless.'
});
function invalid(message) { throw Object.assign(new Error('GUI 样式：' + message), { code: 'GUI_INVALID_ARGUMENT' }); }
function object(value, allowed) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || ![Object.prototype, null].includes(Object.getPrototypeOf(value))) invalid('必须为普通对象');
  for (const key of Reflect.ownKeys(value)) {
    if (typeof key !== 'string' || !allowed.includes(key) || !Object.getOwnPropertyDescriptor(value,key)?.enumerable || !('value' in Object.getOwnPropertyDescriptor(value,key))) invalid('不支持的字段：' + String(key));
  }
}
function option(value, allowed, key) { if (!allowed.includes(value)) invalid(key + ' 不在允许范围'); }
function number(value, min, max, key, integer=true) { if (typeof value !== 'number' || !Number.isFinite(value) || value<min || value>max || integer&&!Number.isInteger(value)) invalid(key + ' 超出允许范围'); }
function hex(value) { if (typeof value !== 'string' || !/^#[0-9a-fA-F]{6}$/.test(value)) invalid('颜色必须为 #RRGGBB'); }
function luminance(color) { const values=[1,3,5].map(i=>parseInt(color.slice(i,i+2),16)/255).map(v=>v<=0.04045?v/12.92:((v+0.055)/1.055)**2.4);return values[0]*0.2126+values[1]*0.7152+values[2]*0.0722; }
function contrast(a,b) { const x=luminance(a),y=luminance(b);return (Math.max(x,y)+0.05)/(Math.min(x,y)+0.05); }
function effectiveColors(theme, mode) {
  const result=Object.fromEntries(colorKeys.map((key,index)=>[key,palettes[theme.preset || 'default'][mode][index]]));
  if(theme.accent) { result.primary=theme.accent; result.onPrimary=contrast(theme.accent,'#000000')>=contrast(theme.accent,'#ffffff')?'#000000':'#ffffff'; }
  return Object.assign(result,theme.colors?.[mode]);
}
export function guiValidateTheme(theme={}) {
  object(theme,['mode','accent','density','preset','colors','typography','spacing','radius','borderWidth','shadow']);
  for(const key of ['mode','density','preset','shadow']) if(key in theme) option(theme[key],GUI_STYLE_CONTRACT.theme[key],key);
  if('accent' in theme) hex(theme.accent);
  if('colors' in theme) {
    object(theme.colors,['light','dark']);
    for(const mode of Object.keys(theme.colors)) { object(theme.colors[mode],colorKeys);Object.values(theme.colors[mode]).forEach(hex); }
    for(const mode of ['light','dark']) {
      const colors=effectiveColors(theme,mode);
      for(const [a,b] of [['text','background'],['text','surface'],['onPrimary','primary']]) if(contrast(colors[a],colors[b])<4.5) invalid(mode+' '+a+'/'+b+' 对比度必须至少 4.5');
    }
  }
  if('typography' in theme) {
    object(theme.typography,['font','size','lineHeight','headingScale']);
    if('font' in theme.typography) option(theme.typography.font,Object.keys(fonts),'font');
    for(const [key,min,max,integer] of [['size',12,20,true],['lineHeight',1.2,2,false],['headingScale',1.1,1.8,false]]) if(key in theme.typography) number(theme.typography[key],min,max,key,integer);
  }
  for(const [key,min,max] of [['spacing',0,32],['radius',0,24],['borderWidth',0,3]]) if(key in theme) number(theme[key],min,max,key);
  return JSON.parse(JSON.stringify(theme));
}
export function guiValidateAppearance(value={}) {
  object(value,Object.keys(GUI_STYLE_CONTRACT.appearance));
  for(const key of ['surface','shadow','textTone','fontWeight','align']) if(key in value) option(value[key],GUI_STYLE_CONTRACT.appearance[key],key);
  for(const [key,min,max] of [['padding',0,32],['radius',0,24],['fontSize',12,40]]) if(key in value) number(value[key],min,max,key);
  if('border' in value && typeof value.border !== 'boolean') invalid('border 必须为布尔值');
  if(value.surface==='primary' && value.textTone==='primary')invalid('primary 背景不能使用 primary 文字');
  return JSON.parse(JSON.stringify(value));
}
export function guiStyleVariables(theme={},mode='light') {
  const value=guiValidateTheme(theme);option(mode,['light','dark'],'mode');
  const colors=effectiveColors(value,mode),type=value.typography || {},result={};
  for(const key of colorKeys) result['--gui-style-'+(key==='onPrimary'?'on-primary':key)]=colors[key];
  Object.assign(result,{
    '--gui-style-font':fonts[type.font || 'system'], '--gui-style-size':(type.size ?? 14)+'px',
    '--gui-style-line-height':String(type.lineHeight ?? 1.5), '--gui-style-heading-scale':String(type.headingScale ?? 1.25),
    '--gui-style-spacing':(value.spacing ?? (value.density==='compact'?8:12))+'px', '--gui-style-radius':(value.radius ?? 10)+'px',
    '--gui-style-border-width':(value.borderWidth ?? 1)+'px', '--gui-style-shadow':shadows[value.shadow || 'none']
  });return result;
}
export function guiAppearanceStyle(appearance={}) {
  const value=guiValidateAppearance(appearance),result={};
  if(value.surface) { result.backgroundColor=value.surface==='transparent'?'transparent':'var(--gui-style-'+({base:'background',surface:'surface',primary:'primary'}[value.surface])+')';if(value.surface==='primary')result.color='var(--gui-style-on-primary)'; }
  if('padding' in value) result.padding=value.padding+'px';
  if('radius' in value) result.borderRadius=value.radius+'px';
  if('border' in value) result.border=value.border?'var(--gui-style-border-width) solid var(--gui-style-border)':'none';
  if(value.shadow) result.boxShadow=shadows[value.shadow];
  if(value.textTone) result.color='var(--gui-style-'+({default:'text',muted:'muted',primary:'primary'}[value.textTone])+')';
  if('fontSize' in value) result.fontSize=value.fontSize+'px';
  if('fontWeight' in value) result.fontWeight=value.fontWeight;
  if(value.align) result.textAlign=value.align;
  return result;
}

// DSH token names audited against ui-theme/design-platform.css and ui-primitives/Button.module.css.
// Leave inverted labels/toast, status colors and disabled states owned by their native components.
export function guiStyleAliases(vars) {
  const groups = {
    background: ['--dsw-alias-bg-base','--dsw-alias-bg-module-platform','--dsw-specific-sidebar-fill'],
    surface: ['--dsw-alias-bg-layer-1','--dsw-alias-bg-layer-2','--dsw-alias-bg-layer-3','--dsw-specific-input-major','--dsw-specific-bubble','--dsw-specific-menu','--dsw-alias-button-elevated-fill','--dsw-alias-button-floating-fill'],
    text: ['--dsw-alias-label-primary','--dsw-alias-label-primary-dimmed','--dsw-alias-label-primary-bluish'],
    muted: ['--dsw-alias-label-secondary','--dsw-alias-label-tertiary','--dsw-alias-label-caption'],
    border: ['--dsw-alias-border-l1','--dsw-alias-border-l2','--dsw-alias-border-l2-darkmode-thin'],
    primary: ['--dsw-static-blue-500','--dsw-alias-brand-primary','--dsw-alias-button-primary-fill','--dsw-alias-button-primary-hover'],
    'on-primary': ['--dsw-alias-label-primary-foreground']
  };
  const result={};
  for(const [color,names] of Object.entries(groups)) {
    const value=vars?.['--gui-style-'+color];hex(value);for(const name of names)result[name]=value;
  }
  for(const name of ['--dsw-alias-interactive-bg-hover','--dsw-alias-interactive-bg-hover-solid','--dsw-alias-interactive-bg-active','--dsw-alias-interactive-bg-hover-accent','--dsw-alias-button-floating-hover'])result[name]=vars['--gui-style-background'];
  return result;
}

// Apply a module wrapper's presentation to its actual persistent business pane.
// Padding belongs only to the outer wrapper; never duplicate it inside the module.
export function guiModuleAppearanceVariables(vars, appearance={}) {
  const value=guiValidateAppearance(appearance),result={...vars};
  for(const color of colorKeys)hex(vars?.['--gui-style-'+(color==='onPrimary'?'on-primary':color)]);
  if(value.surface==='primary' && value.textTone==='primary')invalid('primary 背景不能使用 primary 文字');
  if(value.surface && value.surface!=='transparent') {
    const color=vars['--gui-style-'+({base:'background',surface:'surface',primary:'primary'}[value.surface])];
    result['--gui-style-background']=color;result['--gui-style-surface']=color;
    if(value.surface==='primary')result['--gui-style-text']=result['--gui-style-muted']=vars['--gui-style-on-primary'];
  }
  if(value.textTone)result['--gui-style-text']=result['--gui-style-muted']=vars['--gui-style-'+({default:'text',muted:'muted',primary:'primary'}[value.textTone])];
  if('fontSize' in value)result['--gui-style-size']=value.fontSize+'px';
  if('radius' in value)result['--gui-style-radius']=value.radius+'px';
  if(value.shadow)result['--gui-style-shadow']=shadows[value.shadow];
  if(value.border===false)result['--gui-style-border-width']='0px';
  for(const background of ['background','surface'])if(contrast(result['--gui-style-text'],result['--gui-style-'+background])<4.5)invalid('appearance 文字与 '+background+' 的对比度必须至少 4.5');
  return result;
}
