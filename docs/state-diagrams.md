# State diagrams

These charts describe the inspected code, not a formally verified model of
the tester firmware. Serial writes have no start or STOP acknowledgement.
Retry details, logging internals, and GUI drawing states are omitted.

## CLI active-command lifecycle

```mermaid
stateDiagram-v2
    [*] --> Validate
    Validate --> Rejected: invalid options or missing confirmations
    Validate --> Open: valid live request
    Open --> Identify: port and advisory lock acquired
    Open --> Rejected: open or lock fails
    Identify --> Rejected: unknown or read-only model
    Identify --> Prepare: A10H confirmed
    Prepare --> Start: STOP then CONNECT, baseline, build, discard pending input
    Prepare --> Closed: setup fails before start
    Start --> Sampling: start write returns and session is marked active
    Start --> StopAttempt: partial write or signal
    Sampling --> Sampling: latest status, log, completion check, optional CR adjustment
    Sampling --> StopAttempt: deadline, completion, interrupt, signal, or error
    StopAttempt --> Closed: write succeeds, hardware outcome unconfirmed
    StopAttempt --> Unconfirmed: write fails, tester may still be active
    Rejected --> [*]
    Closed --> [*]
    Unconfirmed --> [*]
```

`commands._run_profile` and `_run_mode` enter `SupervisedSession` before
starting. Named profiles return `EbcModeCommand` and use `device.begin_mode`,
the same start path as generic modes. `device._begin_frame` marks the device
active before the start write; device cleanup therefore also attempts STOP
after a partial write. Identification failure does not send STOP to an unknown
tester. Once the model is identified, preparation can send STOP even if later
packet validation fails. The separate `stop --execute` command skips
identification and the advisory lock.

Sampling deadlines bound status reads and sleeps. They do not preempt blocked
terminal output, filesystem I/O, or a hung host. SIGKILL and lost power do not
run these cleanup paths. Closing the file descriptor is not a hardware stop.

## Bulk-to-float

```mermaid
stateDiagram-v2
    [*] --> BulkUnconfirmed: PbAc start written
    BulkUnconfirmed --> BulkUnconfirmed: completion-only or zero-current samples
    BulkUnconfirmed --> BulkObserved: positive current and no known completion
    BulkObserved --> BulkObserved: ordinary bulk sample
    BulkObserved --> StartFloat: PbAc auto-off 0x1A before bulk deadline
    BulkUnconfirmed --> StopAttempt: bulk deadline
    BulkObserved --> StopAttempt: bulk deadline
    StartFloat --> Float: CCCV start and phase logging complete
    Float --> Float: sample and log
    Float --> StopAttempt: float deadline
    BulkUnconfirmed --> StopAttempt: interrupt, signal, or error
    BulkObserved --> StopAttempt: interrupt, signal, or error
    StartFloat --> StopAttempt: interrupt, signal, or error
    Float --> StopAttempt: interrupt, signal, or error
    StopAttempt --> [*]: best-effort STOP and close
```

`session.run_bulk_to_float_session` requires observed bulk activity before
using `0x1A` as a transition. A stale auto-off alone cannot start float.
The deadline is checked again after output and logging, before the transition.
The float deadline starts after its startup callback completes. A float
startup error takes the cleanup path, not another charging phase.

## Controller connection

```mermaid
stateDiagram-v2
    [*] --> Disconnected
    Disconnected --> Connecting: open serial, send STOP and CONNECT
    Connecting --> Connected: 19-byte response, model index selected
    Connected --> Disconnected: send DISCONNECT and close
    Connecting --> Disconnected: connect button takes disconnect branch
```

This is the normal path in `main.pas`, `btnConnectClick` and `SerialRec`.
The receive handler calls `InterpretPackage` before selecting model scaling.
The GUI's unknown model is a fallback copied from the first model entry.
The error handler displays a connection error; it does not explicitly reset
every connection field, so failure recovery is not drawn as a guaranteed
transition to Disconnected. `csCapture` is declared but is not used on this path.

## Controller step-program runner

```mermaid
stateDiagram-v2
    [*] --> LoadStep: compiled program starts
    LoadStep --> Charge: charge or CCCV step, send Start
    LoadStep --> Discharge: CC or CP step, send Start
    LoadStep --> SoftwareCR: CR step, send CC Start
    LoadStep --> Wait: arm host timer, no start packet
    LoadStep --> Loop: inspect loop count and capacity condition
    Charge --> Break: auto-off, zero current, or enabled charge cutoff
    Discharge --> Break: auto-off or zero current
    SoftwareCR --> SoftwareCR: every third valid sample, send Adjust
    SoftwareCR --> Break: generic completion condition
    Wait --> LoadStep: wait timer completes
    Loop --> LoadStep: restart or advance step list
    Break --> LoadStep: STOP then advance while in program
    LoadStep --> End: END or end of list
    End --> [*]: STOP and leave program
```

The source runner lives in `main.pas`, `LoadStep` and `EBCBreak`.
Its parser lives in `stepform.pas`, `Compile`. This chart omits manual
Skip, manual stop, monitor-only use, and GUI settings changes. It is not a
claim that every documented step-language cutoff is active in the source.
The CLI does not implement this general program runner.
