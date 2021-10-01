#
# This file is part of LiteEth.
#
# Copyright (c) 2015-2021 Florent Kermarrec <florent@enjoy-digital.fr>
# Copyright (c) 2015-2018 Sebastien Bourdeauducq <sb@m-labs.hk>
# Copyright (c) 2021 Leon Schuermann <leon@is.currently.online>
# Copyright (c) 2017 whitequark <whitequark@whitequark.org>
# SPDX-License-Identifier: BSD-2-Clause

import math

from liteeth.common import *

from litex.soc.interconnect.csr import *
from litex.soc.interconnect.csr_eventmanager import *

# MAC SRAM Writer ----------------------------------------------------------------------------------

class LiteEthMACSRAMWriter(Module, AutoCSR):
    def __init__(self, dw, depth, nslots=2, endianness="big", timestamp=None):
        # Endpoint / Signals.
        self.sink      = sink = stream.Endpoint(eth_phy_description(dw))
        self.crc_error = Signal()

        # Parameters Check / Compute.
        assert dw in [32, 64]
        slotbits   = max(int(math.log2(nslots)), 1)
        lengthbits = bits_for(depth * dw//8)

        # CSRs.
        self._slot   = CSRStatus(slotbits)
        self._length = CSRStatus(lengthbits)
        self._errors = CSRStatus(32)

        # Optional Timestamp of the incoming packets and expose value to software.
        if timestamp is not None:
            timestampbits   = len(timestamp)
            self._timestamp = CSRStatus(timestampbits)

        # Event Manager.
        self.submodules.ev = EventManager()
        self.ev.available  = EventSourceLevel()
        self.ev.finalize()

        # # #

        write   = Signal()
        errors  = self._errors.status

        slot       = Signal(slotbits)
        length     = Signal(lengthbits)
        length_inc = Signal(4)

        # Sink is already ready: packets are dropped when no slot is available.
        sink.ready.reset = 1

        # Decode Length increment from from last_be.
        if dw == 32:
            self.comb += Case(sink.last_be, {
                0b0001   : length_inc.eq(1),
                0b0010   : length_inc.eq(2),
                0b0100   : length_inc.eq(3),
               "default" : length_inc.eq(4)
            })
        else:
            self.comb += Case(sink.last_be, {
                0b00000001 : length_inc.eq(1),
                0b00000010 : length_inc.eq(2),
                0b00000100 : length_inc.eq(3),
                0b00001000 : length_inc.eq(4),
                0b00010000 : length_inc.eq(5),
                0b00100000 : length_inc.eq(6),
                0b01000000 : length_inc.eq(7),
                "default"  : length_inc.eq(8)
            })

        # Status FIFO.
        stat_fifo_layout = [("slot", slotbits), ("length", lengthbits)]
        if timestamp is not None:
            stat_fifo_layout += [("timestamp", timestampbits)]
        self.submodules.stat_fifo = stat_fifo = stream.SyncFIFO(stat_fifo_layout, nslots)

        # FSM.
        self.submodules.fsm = fsm = FSM(reset_state="WRITE")
        fsm.act("WRITE",
            If(sink.valid,
                If(stat_fifo.sink.ready,
                    write.eq(1),
                    NextValue(length, length + length_inc),
                    If(length >= eth_mtu,
                         NextState("DISCARD-REMAINING")
                    ),
                    If(sink.last,
                        If((sink.error & sink.last_be) != 0,
                            NextState("DISCARD")
                        ).Else(
                            NextState("TERMINATE")
                        )
                    )
                ).Else(
                    NextValue(errors, errors + 1),
                    NextState("DISCARD-REMAINING")
                )
            )
        )
        fsm.act("DISCARD-REMAINING",
            If(sink.valid & sink.last,
                If((sink.error & sink.last_be) != 0,
                    NextState("DISCARD")
                ).Else(
                    NextState("TERMINATE")
                )
            )
        )
        fsm.act("DISCARD",
            NextValue(length, 0),
            NextState("WRITE")
        )
        fsm.act("TERMINATE",
            stat_fifo.sink.valid.eq(1),
            stat_fifo.sink.slot.eq(slot),
            stat_fifo.sink.length.eq(length),
            NextValue(length, 0),
            NextValue(slot, slot + 1),
            NextState("WRITE")
        )

        self.comb += [
            stat_fifo.source.ready.eq(self.ev.available.clear),
            self.ev.available.trigger.eq(stat_fifo.source.valid),
            self._slot.status.eq(stat_fifo.source.slot),
            self._length.status.eq(stat_fifo.source.length),
        ]
        if timestamp is not None:
            # Latch Timestamp on start of packet.
            self.sync += If(sink.length == 0, stat_fifo.sink.timestamp.eq(timestamp))
            self.comb += self._timestamp.status.eq(stat_fifo.source.timestamp)

        # Memory.
        mems  = [None] * nslots
        ports = [None] * nslots
        for n in range(nslots):
            mems[n] = Memory(dw, depth)
            ports[n] = mems[n].get_port(write_capable=True)
            self.specials += ports[n]
        self.mems = mems
        data = reverse_bytes(sink.data) if endianness == "big" else sink.data

        cases = {}
        for n, port in enumerate(ports):
            cases[n] = [
                ports[n].adr.eq(length[int(math.log2(dw//8)):]),
                ports[n].dat_w.eq(data),
                If(sink.valid & write,
                    ports[n].we.eq(0xf)
                )
            ]
        self.comb += Case(slot, cases)

# MAC SRAM Reader ----------------------------------------------------------------------------------

class LiteEthMACSRAMReader(Module, AutoCSR):
    def __init__(self, dw, depth, nslots=2, endianness="big", timestamp=None):
        assert dw in [32, 64]
        self.source = source = stream.Endpoint(eth_phy_description(dw))

        slotbits        = max(int(math.log2(nslots)), 1)
        lengthbits      = bits_for(depth * dw//8)
        self.lengthbits = lengthbits

        self._start  = CSR()
        self._ready  = CSRStatus()
        self._level  = CSRStatus(int(math.log2(nslots)) + 1)
        self._slot   = CSRStorage(slotbits,   reset_less=True)
        self._length = CSRStorage(lengthbits, reset_less=True)

        if timestamp is not None:
            # Timestamp the outgoing packets when a Timestamp source is provided
            # and expose value to a software.
            timestampbits        = len(timestamp)
            self._timestamp_slot = CSRStatus(slotbits)
            self._timestamp      = CSRStatus(timestampbits)

        self.submodules.ev = EventManager()
        self.ev.done       = EventSourcePulse() if timestamp is None else EventSourceLevel()
        self.ev.finalize()

        # # #

        start = Signal()

        # Command FIFO
        cmd_fifo = stream.SyncFIFO([("slot", slotbits), ("length", lengthbits)], nslots)
        self.submodules += cmd_fifo
        self.comb += [
            cmd_fifo.sink.valid.eq(self._start.re),
            cmd_fifo.sink.slot.eq(self._slot.storage),
            cmd_fifo.sink.length.eq(self._length.storage),
            self._ready.status.eq(cmd_fifo.sink.ready),
            self._level.status.eq(cmd_fifo.level)
        ]

        # Status FIFO (Only added when Timestamping).
        if timestamp is not None:
            stat_fifo_layout = [("slot", slotbits), ("timestamp", timestampbits)]
            stat_fifo = stream.SyncFIFO(stat_fifo_layout, nslots)
            self.submodules += stat_fifo
            self.comb += stat_fifo.source.ready.eq(self.ev.done.clear)
            self.comb += self._timestamp_slot.status.eq(stat_fifo.source.slot)
            self.comb += self._timestamp.status.eq(stat_fifo.source.timestamp)

        # Length computation
        read_address = Signal(lengthbits)
        counter      = Signal(lengthbits)

        # FSM
        self.submodules.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            NextValue(counter, 0),
            If(cmd_fifo.source.valid,
                start.eq(1),
                NextState("SEND")
            )
        )

        # Encode Length to last_be.
        length_lsb = cmd_fifo.source.length[0:int(math.log2(dw//8))]
        if dw == 32:
            self.comb += If(source.last,
                Case(length_lsb, {
                    1         : source.last_be.eq(0b0001),
                    2         : source.last_be.eq(0b0010),
                    3         : source.last_be.eq(0b0100),
                    "default" : source.last_be.eq(0b1000),
                })
            )
        else:
            self.comb += If(source.last,
                Case(length_lsb, {
                    1         : source.last_be.eq(0b00000001),
                    2         : source.last_be.eq(0b00000010),
                    3         : source.last_be.eq(0b00000100),
                    4         : source.last_be.eq(0b00001000),
                    5         : source.last_be.eq(0b00010000),
                    6         : source.last_be.eq(0b00100000),
                    7         : source.last_be.eq(0b01000000),
                    "default" : source.last_be.eq(0b10000000),
                })
            )

        fsm.act("SEND",
            source.valid.eq(1),
            source.last.eq(counter >= (cmd_fifo.source.length - dw//8)),
            read_address.eq(counter),
            If(source.ready,
                read_address.eq(counter + dw//8),
                NextValue(counter, counter + dw//8),
                If(source.last,
                    NextState("END")
                )
            )
        )
        fsm.act("END",
            self.ev.done.trigger.eq(1),
            cmd_fifo.source.ready.eq(1),
            NextState("IDLE")
        )

        if timestamp is not None:
            # Latch Timestamp on start of outgoing packet.
            self.sync += If(start, stat_fifo.sink.timestamp.eq(timestamp))
            self.comb += stat_fifo.sink.valid.eq(fsm.ongoing("END"))
            self.comb += stat_fifo.sink.slot.eq(cmd_fifo.source.slot)
            # Trigger event when Status FIFO has contents (Override FSM assignment).
            self.comb += self.ev.done.trigger.eq(stat_fifo.source.valid)

        # Memory
        rd_slot = cmd_fifo.source.slot
        mems    = [None]*nslots
        ports   = [None]*nslots
        for n in range(nslots):
            mems[n]  = Memory(dw, depth)
            ports[n] = mems[n].get_port()
            self.specials += ports[n]
        self.mems = mems
        data = Signal().like(source.data)

        cases = {}
        for n, port in enumerate(ports):
            self.comb += ports[n].adr.eq(read_address[int(math.log2(dw//8)):])
            cases[n] = [data.eq(port.dat_r)]

        self.comb += [
            Case(rd_slot, cases),
            source.data.eq(reverse_bytes(data) if endianness == "big" else data),
        ]

# MAC SRAM -----------------------------------------------------------------------------------------

class LiteEthMACSRAM(Module, AutoCSR):
    def __init__(self, dw, depth, nrxslots, ntxslots, endianness, timestamp=None):
        self.submodules.writer = LiteEthMACSRAMWriter(dw, depth, nrxslots, endianness, timestamp)
        self.submodules.reader = LiteEthMACSRAMReader(dw, depth, ntxslots, endianness, timestamp)
        self.submodules.ev     = SharedIRQ(self.writer.ev, self.reader.ev)
        self.sink, self.source = self.writer.sink, self.reader.source
