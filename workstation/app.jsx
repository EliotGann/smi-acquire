import React, {useEffect, useState} from 'react';
import {createRoot} from 'react-dom/client';
import FinchHeader from 'finch-header';

const token = location.hash.slice(1) || sessionStorage.getItem('session');
if (token) sessionStorage.setItem('session', token);
history.replaceState(null, '', location.pathname);

async function request(path, signal, method = 'GET') {
    const response = await fetch(`/api/${path}`, {method, signal, cache: 'no-store',
        headers: {'X-Workstation-Session': token || ''}});
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

function Camera({simulation}) {
    const [url, error] = usePoll('frame', 300, true);
    const [message, setMessage] = useState(''), [pending, setPending] = useState(false);
    async function simulate(action) {
        setPending(true);
        try {
            const state = await (await request(action, undefined, 'POST')).json();
            setMessage(state.history[0].message);
        } catch (err) { setMessage(err.message); }
        finally {setPending(false);}
    }
    return <section><h2>On-axis camera</h2><p>{simulation ? 'Synthetic image' : 'Direct EPICS readback'}</p>
        {url ? <img className="camera" src={url} alt="Latest on-axis camera frame"/> : <p>{error || 'Waiting for a frame…'}</p>}
        {simulation && <fieldset><legend>Simulated immediate plans</legend>
            <button disabled={pending} onClick={() => simulate('jog')}>Jog X +0.1</button>
            <button disabled={pending} onClick={() => simulate('exposure')}>Toggle exposure</button>
            <output>{message}</output></fieldset>}</section>;
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
        <main><h1>{page}</h1>{page === 'Camera' ? <Camera simulation={state?.mode === 'simulation'}/> : state?.connected ? <>
            {!state.errors.queue && <Items title="Pending queue" items={state.queue}/>}
            {!state.errors.history && <Items title="Recent history" items={state.history}/>}
            <section><h2>RunEngine output</h2><pre>{state.errors.console ? 'Console unavailable' : state.console || 'Waiting for output…'}</pre></section>
        </> : <p>Waiting for QueueServer.</p>}</main></div></>;
}

createRoot(document.getElementById('root')).render(<App/>);
