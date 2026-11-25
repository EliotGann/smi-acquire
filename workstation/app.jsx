import React, {useEffect, useState} from 'react';
import {createRoot} from 'react-dom/client';
import FinchHeader from 'finch-header';

const token = location.hash.slice(1) || sessionStorage.getItem('session');
if (token) sessionStorage.setItem('session', token);
history.replaceState(null, '', location.pathname);

async function request(path, signal, method = 'GET', body) {
    const response = await fetch(`/api/${path}`, {method, signal, cache: 'no-store',
        body: body && JSON.stringify(body),
        headers: {'X-Workstation-Session': token || '', 'Content-Type': 'application/json'}});
    if (response.status === 409) throw Error((await response.json()).error);
    if (!response.ok) throw Error(response.status === 403 ? 'Reopen from workstation launcher' :
        `Unavailable (${response.status})`);
    return response;
}

// Await each request before scheduling another. Tabs that unmount release their subscription.
function usePoll(path, period, image = false) {
    const [value, setValue] = useState(null), [error, setError] = useState('');
    useEffect(() => {
        const controller = new AbortController();
        let timer, url, delay = period;
        async function tick() {
            try {
                const r = await request(path, controller.signal);
                const result = image ? await r.blob() : await r.json();
                if (controller.signal.aborted) return;
                if (url) URL.revokeObjectURL(url);
                setValue(image ? (url = URL.createObjectURL(result)) : result);
                setError(''); delay = period;
            } catch (err) {
                if (!controller.signal.aborted) {setValue(null); setError(err.message);}
                delay = Math.min(10000, delay * 2);
            }
            if (!controller.signal.aborted) timer = setTimeout(tick, delay);
        }
        tick();
        return () => {controller.abort(); clearTimeout(timer); if (url) URL.revokeObjectURL(url);};
    }, [path, period, image]);
    return [value, error];
}

function Camera({state}) {
    const [url, error] = usePoll('frame', 300, true);
    const [message, setMessage] = useState(''), [pending, setPending] = useState(false);
    const [seconds, setSeconds] = useState('');
    const caps = state?.capabilities, command = state?.command;
    const ready = state?.connected && state?.idle && !pending &&
        !['sending', 'accepted', 'unknown'].includes(command?.state);
    const allowed = (plan, device) => ready && caps?.plans.includes(plan) && caps.movable.includes(device);
    const exposureBlocker = !state?.connected ? 'Waiting for QueueServer connection' :
        !caps ? 'Allowed plans and devices unavailable' :
        !caps.exposure ? 'No exposure device bound' :
        !caps.plans.includes('mv') ? 'mv is not allowed' :
        !caps.movable.includes(caps.exposure) ? `${caps.exposure} is not listed as movable` :
        pending ? 'Sending request' : !state.idle ? state.idle_blocker || 'Worker not ready' :
        !ready ? `Previous command: ${command.state}; check history` :
        state.exposure_rbv == null ? 'Exposure readback unavailable' :
        !(+seconds >= .0001 && +seconds <= 2) ? 'Enter 0.0001 to 2 seconds' : '';
    async function send(action, value) {
        setPending(true);
        try {
            const cmd = await (await request(action, undefined, 'POST', {value})).json();
            setMessage(`Request ${cmd.tag}: ${cmd.state}`);
        } catch (err) { setMessage(err.message); }
        finally {setPending(false);}
    }
    return <section><h2>On-axis camera</h2><p>{state?.mode === 'simulation' ? 'Synthetic image' : 'Direct EPICS readback'}</p>
        {url ? <img className="camera" src={url} alt="Latest on-axis camera frame"/> : <p>{error || 'Waiting for a frame…'}</p>}
        <fieldset><legend>Immediate plans</legend><p>Exposure readback: {state?.exposure_rbv == null ? 'unavailable' : `${state.exposure_rbv} s`}</p>
            <label>Requested exposure (s) <input type="number" min="0.0001" max="2" step="any" value={seconds} onChange={e=>setSeconds(e.target.value)}/></label>
            <button disabled={Boolean(exposureBlocker)}
                onClick={()=>send('exposure', +seconds)}>Set exposure</button>
            {exposureBlocker && <small>{exposureBlocker}</small>}
            {caps?.axis && <p>{[-1,1].map(sign=><button key={sign} disabled={!allowed('mvr', caps.axis)} onClick={()=>send('jog', sign*caps.step)}>
                Jog {caps.axis} {sign>0?'+':'−'}{caps.step} {caps.units}</button>)}</p>}
            <output>{message}</output>{command && <p role="status">{command.plan} {command.device} {command.value}: {command.state} {command.outcome} {command.message}
                <small>Item UID: {command.uid || 'not available'}{state.confirmation && ` · Exposure RBV: ${state.confirmation}`}</small></p>}
        </fieldset></section>;
}

function Capabilities({caps}) {
    const [query, setQuery] = useState('');
    if (!caps) return <p>Allowed lists unavailable.</p>;
    const devices = caps.devices.filter(d=>d.toLowerCase().includes(query.toLowerCase()));
    return <details><summary>Capabilities for this workstation login</summary>
        <p>mv: {caps.plans.includes('mv')?'allowed':'missing'} · mvr: {caps.plans.includes('mvr')?'allowed':'missing'}</p>
        {[['Exposure', caps.exposure], ['Jog', caps.axis]].map(([label,device])=><p key={label}>{label}: {device ? `${device} (${caps.movable.includes(device)?'movable':'not listed as movable'})` : 'not bound'}</p>)}
        <details><summary>Allowed plans ({caps.plans.length})</summary>{caps.plans.join(', ')}</details>
        <label>Find a device <input value={query} onChange={e=>setQuery(e.target.value)} placeholder="e.g. acquire_time"/></label>
        <p>{devices.length} matches (first 100)</p><pre>{devices.slice(0,100).join('\n')}</pre></details>;
}

function Items({title, items = []}) {
    return <section><h2>{title}</h2>{items.length ? <ol>{items.map(i => <li key={i.uid}>
        <strong>{i.name}</strong> {i.outcome} {i.message}<small>{i.uid}</small></li>)}</ol> : <p>No items.</p>}</section>;
}

function App() {
    const [page, navigate] = useState('Camera');
    const [state, error] = usePoll('state', 1000);
    return <><FinchHeader title="SMI Acquire" pageTitle="Workstation" logoUrl="" rightSlot={<b>{state?.mode || 'Connecting'}</b>}/>
        <div className="status" role="status">{state?.connected ? `Manager: ${state.manager} · RE: ${state.re || '—'} · Worker: ${state.environment || '—'} · Pending: ${state.count} · Running: ${state.running || 'none'}` : error || 'QueueServer disconnected'}
            {Object.entries(state?.errors || {}).map(([key, msg]) => <span key={key}> · {key}: {msg} (data unavailable)</span>)}</div>
        <div className="workspace"><nav aria-label="Workstation tools"><details open><summary>Acquisition</summary>
            {Object.entries({'Align & Samples':'Camera', 'RunEngine':'Queue & Console'}).map(([section, tab]) =>
                <details key={tab} open><summary>{section}</summary><button aria-current={page === tab ? 'page' : undefined}
                    onClick={() => navigate(tab)}>{tab}</button></details>)}</details></nav>
        <main><h1>{page}</h1>{page === 'Camera' ? <Camera state={state}/> : state?.connected ? <>
            <Capabilities caps={state.capabilities}/>
            {!state.errors.queue && <Items title="Pending queue" items={state.queue}/>}
            {!state.errors.history && <Items title="Recent history" items={state.history}/>}
            <section><h2>RunEngine output</h2><pre>{state.errors.console ? 'Console unavailable' : state.console || 'Waiting for output…'}</pre></section>
        </> : <p>Waiting for QueueServer.</p>}</main></div></>;
}

createRoot(document.getElementById('root')).render(<App/>);
