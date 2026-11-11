//+------------------------------------------------------------------+
//| State.mqh - file-backed idempotency, position and daily state    |
//|                                                                  |
//| Files live under the MQL5 common folder so the EA can survive a  |
//| terminal restart.  The Strategy Tester uses in-memory state only |
//| so every run starts from the same baseline.                      |
//+------------------------------------------------------------------+
#ifndef TA_STATE_MQH
#define TA_STATE_MQH

#include <TradingAgents/Types.mqh>

class TAState
{
private:
   string           m_dir;
   bool             m_persist;

   string           m_key[];
   long             m_key_ts[];
   ulong            m_key_ticket[];
   int              m_key_status[];
   int              m_key_count;

   TAPositionState  m_pos[];
   int              m_pos_count;

   TATradeRecord    m_records[];
   int              m_record_count;

   long             m_day_id;
   double           m_day_start_equity;
   double           m_day_realized_r;
   int              m_consecutive_losses;
   bool             m_locked;
   string           m_lock_reason;
   bool             m_halted;
   string           m_halt_reason;
   long             m_halt_until;

   string Path(const string name) const
   {
      return m_dir + "\\" + name;
   }

   string Clean(const string value) const
   {
      string out = value;
      StringReplace(out, "|", "/");
      StringReplace(out, "\r", " ");
      StringReplace(out, "\n", " ");
      return out;
   }

   void ResetKeys()
   {
      m_key_count = 0;
      ArrayResize(m_key, 0);
      ArrayResize(m_key_ts, 0);
      ArrayResize(m_key_ticket, 0);
      ArrayResize(m_key_status, 0);
   }

public:
   TAState()
   {
      m_dir = "TradingAgents";
      m_persist = false;
      m_key_count = 0;
      m_pos_count = 0;
      m_record_count = 0;
      m_day_id = 0;
      m_day_start_equity = 0.0;
      m_day_realized_r = 0.0;
      m_consecutive_losses = 0;
      m_locked = false;
      m_lock_reason = "";
      m_halted = false;
      m_halt_reason = "";
      m_halt_until = 0;
   }

   void Init(const string directory, const bool persist)
   {
      m_dir = directory;
      m_persist = persist;
      m_key_count = 0;
      m_pos_count = 0;
      m_record_count = 0;
      m_day_id = 0;
      m_day_start_equity = 0.0;
      m_day_realized_r = 0.0;
      m_consecutive_losses = 0;
      m_locked = false;
      m_lock_reason = "";
      m_halted = false;
      m_halt_reason = "";
      m_halt_until = 0;
   }

   bool Persist() const
   {
      return m_persist;
   }

   string Directory() const
   {
      return m_dir;
   }

   //---------------------------------------------------------------- state file
   void SaveState()
   {
      if(!m_persist)
         return;
      int handle = FileOpen(Path("ea_state.csv"),
                            FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
      if(handle == INVALID_HANDLE)
      {
         PrintFormat("TAState: cannot write state file: %d", GetLastError());
         return;
      }
      FileWriteString(handle, StringFormat("day_id|%I64d\r\n", m_day_id));
      FileWriteString(handle, StringFormat("day_start_equity|%.2f\r\n", m_day_start_equity));
      FileWriteString(handle, StringFormat("day_realized_r|%.4f\r\n", m_day_realized_r));
      FileWriteString(handle, StringFormat("consecutive_losses|%d\r\n", m_consecutive_losses));
      FileWriteString(handle, StringFormat("locked|%d\r\n", m_locked ? 1 : 0));
      FileWriteString(handle, "lock_reason|" + Clean(m_lock_reason) + "\r\n");
      FileWriteString(handle, StringFormat("halted|%d\r\n", m_halted ? 1 : 0));
      FileWriteString(handle, "halt_reason|" + Clean(m_halt_reason) + "\r\n");
      FileWriteString(handle, StringFormat("halt_until|%I64d\r\n", m_halt_until));
      FileFlush(handle);
      FileClose(handle);
   }

   void LoadState()
   {
      if(!m_persist)
         return;
      int handle = FileOpen(Path("ea_state.csv"),
                            FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON);
      if(handle == INVALID_HANDLE)
         return;
      while(!FileIsEnding(handle))
      {
         string line = FileReadString(handle);
         if(StringLen(line) == 0)
            continue;
         string parts[];
         if(StringSplit(line, StringGetCharacter("|", 0), parts) < 2)
            continue;
         string key = parts[0];
         string value = parts[1];
         if(key == "day_id")
            m_day_id = StringToInteger(value);
         else if(key == "day_start_equity")
            m_day_start_equity = StringToDouble(value);
         else if(key == "day_realized_r")
            m_day_realized_r = StringToDouble(value);
         else if(key == "consecutive_losses")
            m_consecutive_losses = (int)StringToInteger(value);
         else if(key == "locked")
            m_locked = (StringToInteger(value) != 0);
         else if(key == "lock_reason")
            m_lock_reason = value;
         else if(key == "halted")
            m_halted = (StringToInteger(value) != 0);
         else if(key == "halt_reason")
            m_halt_reason = value;
         else if(key == "halt_until")
            m_halt_until = StringToInteger(value);
      }
      FileClose(handle);
   }

   //--------------------------------------------------------------- seen keys
   void LoadKeys()
   {
      ResetKeys();
      if(!m_persist)
         return;
      int handle = FileOpen(Path("seen_keys.csv"),
                            FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON);
      if(handle == INVALID_HANDLE)
         return;
      while(!FileIsEnding(handle))
      {
         string line = FileReadString(handle);
         if(StringLen(line) == 0)
            continue;
         string parts[];
         if(StringSplit(line, StringGetCharacter(",", 0), parts) < 4)
            continue;
         if(m_key_count >= TA_MAX_SEEN)
            break;
         ArrayResize(m_key, m_key_count + 1);
         ArrayResize(m_key_ts, m_key_count + 1);
         ArrayResize(m_key_ticket, m_key_count + 1);
         ArrayResize(m_key_status, m_key_count + 1);
         m_key[m_key_count] = parts[0];
         m_key_ts[m_key_count] = StringToInteger(parts[1]);
         m_key_ticket[m_key_count] = (ulong)StringToInteger(parts[2]);
         m_key_status[m_key_count] = (int)StringToInteger(parts[3]);
         m_key_count++;
      }
      FileClose(handle);
      PruneKeys((long)TimeGMT());
   }

   void SaveKeys()
   {
      if(!m_persist)
         return;
      int handle = FileOpen(Path("seen_keys.csv"),
                            FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
      if(handle == INVALID_HANDLE)
      {
         PrintFormat("TAState: cannot write seen keys: %d", GetLastError());
         return;
      }
      for(int i = 0; i < m_key_count; i++)
         FileWriteString(handle, StringFormat("%s,%I64d,%I64d,%d\r\n",
                        m_key[i], m_key_ts[i], (long)m_key_ticket[i], m_key_status[i]));
      FileFlush(handle);
      FileClose(handle);
   }

   void PruneKeys(const long now)
   {
      if(m_key_count <= 0)
         return;
      int out = 0;
      for(int i = 0; i < m_key_count; i++)
      {
         if(now - m_key_ts[i] > 86400)
            continue;
         m_key[out] = m_key[i];
         m_key_ts[out] = m_key_ts[i];
         m_key_ticket[out] = m_key_ticket[i];
         m_key_status[out] = m_key_status[i];
         out++;
      }
      m_key_count = out;
      if(m_key_count > TA_MAX_SEEN)
      {
         int excess = m_key_count - TA_MAX_SEEN;
         for(int i = excess; i < m_key_count; i++)
         {
            m_key[i - excess] = m_key[i];
            m_key_ts[i - excess] = m_key_ts[i];
            m_key_ticket[i - excess] = m_key_ticket[i];
            m_key_status[i - excess] = m_key_status[i];
         }
         m_key_count = TA_MAX_SEEN;
      }
   }

   bool FindKey(const string key, ulong &ticket, int &status)
   {
      long now = (long)TimeGMT();
      for(int i = 0; i < m_key_count; i++)
      {
         if(m_key[i] != key)
            continue;
         if(now - m_key_ts[i] > 86400)
            return false;
         ticket = m_key_ticket[i];
         status = m_key_status[i];
         return true;
      }
      return false;
   }

   void RememberKey(const string key, const ulong ticket, const int status)
   {
      long now = (long)TimeGMT();
      PruneKeys(now);
      for(int i = 0; i < m_key_count; i++)
      {
         if(m_key[i] == key)
         {
            m_key_ts[i] = now;
            m_key_ticket[i] = ticket;
            m_key_status[i] = status;
            SaveKeys();
            return;
         }
      }
      if(m_key_count >= TA_MAX_SEEN)
      {
         for(int i = 1; i < m_key_count; i++)
         {
            m_key[i - 1] = m_key[i];
            m_key_ts[i - 1] = m_key_ts[i];
            m_key_ticket[i - 1] = m_key_ticket[i];
            m_key_status[i - 1] = m_key_status[i];
         }
         m_key_count = TA_MAX_SEEN - 1;
      }
      ArrayResize(m_key, m_key_count + 1);
      ArrayResize(m_key_ts, m_key_count + 1);
      ArrayResize(m_key_ticket, m_key_count + 1);
      ArrayResize(m_key_status, m_key_count + 1);
      m_key[m_key_count] = key;
      m_key_ts[m_key_count] = now;
      m_key_ticket[m_key_count] = ticket;
      m_key_status[m_key_count] = status;
      m_key_count++;
      SaveKeys();
   }

   //--------------------------------------------------------------- positions
   void LoadPositions()
   {
      m_pos_count = 0;
      ArrayResize(m_pos, 0);
      if(!m_persist)
         return;
      int handle = FileOpen(Path("positions.csv"),
                            FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON);
      if(handle == INVALID_HANDLE)
         return;
      while(!FileIsEnding(handle))
      {
         string line = FileReadString(handle);
         if(StringLen(line) == 0)
            continue;
         string parts[];
         if(StringSplit(line, StringGetCharacter(",", 0), parts) < 15)
            continue;
         if(m_pos_count >= TA_MAX_POSITIONS)
            break;
         TAPositionState state;
         state.ticket = 0;
         state.strategy = TA_STRAT_NONE;
         state.direction = 0;
         state.entry = 0.0;
         state.initial_stop = 0.0;
         state.initial_risk = 0.0;
         state.target = 0.0;
         state.risk_money = 0.0;
         state.opened_at = 0;
         state.mfe_r = 0.0;
         state.mae_r = 0.0;
         state.partial_done = false;
         state.breakeven_done = false;
         state.adopted = false;
         state.bars_held = 0;
         state.pending_exit_reason = TA_EXIT_UNKNOWN;
         state.close_requested = false;
         state.entry_spread_points = 0.0;
         state.initial_volume = 0.0;
         state.last_modify_ms = GetTickCount();
         state.ticket = (ulong)StringToInteger(parts[0]);
         state.strategy = (int)StringToInteger(parts[1]);
         state.direction = (int)StringToInteger(parts[2]);
         state.entry = StringToDouble(parts[3]);
         state.initial_stop = StringToDouble(parts[4]);
         state.initial_risk = StringToDouble(parts[5]);
         state.target = StringToDouble(parts[6]);
         state.risk_money = StringToDouble(parts[7]);
         state.opened_at = (datetime)StringToInteger(parts[8]);
         state.mfe_r = StringToDouble(parts[9]);
         state.mae_r = StringToDouble(parts[10]);
         state.partial_done = (StringToInteger(parts[11]) != 0);
         state.breakeven_done = (StringToInteger(parts[12]) != 0);
         state.adopted = (StringToInteger(parts[13]) != 0);
         state.bars_held = (int)StringToInteger(parts[14]);
         if(ArraySize(parts) > 15)
            state.pending_exit_reason = (int)StringToInteger(parts[15]);
         if(ArraySize(parts) > 16)
            state.close_requested = (StringToInteger(parts[16]) != 0);
         if(ArraySize(parts) > 17)
            state.entry_spread_points = StringToDouble(parts[17]);
         if(ArraySize(parts) > 18)
            state.initial_volume = StringToDouble(parts[18]);
         ArrayResize(m_pos, m_pos_count + 1);
         m_pos[m_pos_count] = state;
         m_pos_count++;
      }
      FileClose(handle);
   }

   void SavePositions()
   {
      if(!m_persist)
         return;
      int handle = FileOpen(Path("positions.csv"),
                            FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
      if(handle == INVALID_HANDLE)
         return;
      for(int i = 0; i < m_pos_count; i++)
      {
         FileWriteString(handle, StringFormat(
            "%I64d,%d,%d,%.5f,%.5f,%.5f,%.5f,%.2f,%I64d,%.4f,%.4f,%d,%d,%d,%d,%d,%d,%.1f,%.2f\r\n",
            (long)m_pos[i].ticket, m_pos[i].strategy, m_pos[i].direction,
            m_pos[i].entry, m_pos[i].initial_stop, m_pos[i].initial_risk,
            m_pos[i].target, m_pos[i].risk_money, (long)m_pos[i].opened_at,
            m_pos[i].mfe_r, m_pos[i].mae_r, m_pos[i].partial_done ? 1 : 0,
            m_pos[i].breakeven_done ? 1 : 0, m_pos[i].adopted ? 1 : 0,
            m_pos[i].bars_held, m_pos[i].pending_exit_reason,
            m_pos[i].close_requested ? 1 : 0, m_pos[i].entry_spread_points,
            m_pos[i].initial_volume));
      }
      FileFlush(handle);
      FileClose(handle);
   }

   int FindPosition(const ulong ticket, int &index)
   {
      for(int i = 0; i < m_pos_count; i++)
      {
         if(m_pos[i].ticket == ticket)
         {
            index = i;
            return i;
         }
      }
      index = -1;
      return -1;
   }

   bool PositionAt(const int index, TAPositionState &state) const
   {
      if(index < 0 || index >= m_pos_count)
         return false;
      state = m_pos[index];
      return true;
   }

   int PositionCount() const
   {
      return m_pos_count;
   }

   void AddOrUpdatePosition(const TAPositionState &state, const bool save = true)
   {
      int index = -1;
      FindPosition(state.ticket, index);
      if(index >= 0)
      {
         m_pos[index] = state;
      }
      else
      {
         if(m_pos_count >= TA_MAX_POSITIONS)
            return;
         ArrayResize(m_pos, m_pos_count + 1);
         m_pos[m_pos_count] = state;
         m_pos_count++;
      }
      if(save)
         SavePositions();
   }

   void RemovePosition(const ulong ticket)
   {
      int index = -1;
      FindPosition(ticket, index);
      if(index < 0)
         return;
      for(int i = index + 1; i < m_pos_count; i++)
         m_pos[i - 1] = m_pos[i];
      m_pos_count--;
      ArrayResize(m_pos, m_pos_count);
      SavePositions();
   }

   //----------------------------------------------------------------- records
   void AddRecord(const TATradeRecord &record)
   {
      if(m_record_count >= TA_MAX_RECORDS)
      {
         for(int i = 1; i < m_record_count; i++)
            m_records[i - 1] = m_records[i];
         m_record_count = TA_MAX_RECORDS - 1;
      }
      ArrayResize(m_records, m_record_count + 1);
      m_records[m_record_count] = record;
      m_record_count++;
   }

   int RecordCount() const
   {
      return m_record_count;
   }

   bool RecordAt(const int index, TATradeRecord &record) const
   {
      if(index < 0 || index >= m_record_count)
         return false;
      record = m_records[index];
      return true;
   }

   //------------------------------------------------------------------- daily
   void RollDay(const long day_id, const double equity)
   {
      if(m_day_id == 0)
      {
         m_day_id = day_id;
         m_day_start_equity = equity;
         m_day_realized_r = 0.0;
         m_consecutive_losses = 0;
         m_locked = false;
         m_lock_reason = "";
         SaveState();
         return;
      }
      if(day_id == m_day_id)
         return;
      m_day_id = day_id;
      m_day_start_equity = equity;
      m_day_realized_r = 0.0;
      m_consecutive_losses = 0;
      m_locked = false;
      m_lock_reason = "";
      if(m_halted && m_halt_until > 0 && (long)TimeGMT() >= m_halt_until)
      {
         m_halted = false;
         m_halt_reason = "";
         m_halt_until = 0;
      }
      SaveState();
   }

   void RegisterResult(const double r_multiple)
   {
      m_day_realized_r += r_multiple;
      if(r_multiple < 0.0)
         m_consecutive_losses++;
      else
         m_consecutive_losses = 0;
      SaveState();
   }

   void Lock(const string reason)
   {
      m_locked = true;
      m_lock_reason = reason;
      SaveState();
   }

   void Unlock()
   {
      m_locked = false;
      m_lock_reason = "";
      SaveState();
   }

   bool Locked() const
   {
      return m_locked;
   }

   string LockReason() const
   {
      return m_lock_reason;
   }

   double DayStartEquity() const
   {
      return m_day_start_equity;
   }

   double DayRealizedR() const
   {
      return m_day_realized_r;
   }

   int ConsecutiveLosses() const
   {
      return m_consecutive_losses;
   }

   long DayId() const
   {
      return m_day_id;
   }

   void Halt(const string reason, const long until)
   {
      m_halted = true;
      m_halt_reason = reason;
      m_halt_until = until;
      SaveState();
   }

   void Resume()
   {
      m_halted = false;
      m_halt_reason = "";
      m_halt_until = 0;
      SaveState();
   }

   bool Halted()
   {
      if(m_halted && m_halt_until > 0 && (long)TimeGMT() >= m_halt_until)
      {
         m_halted = false;
         m_halt_reason = "";
         m_halt_until = 0;
         SaveState();
      }
      return m_halted;
   }

   string HaltReason() const
   {
      return m_halt_reason;
   }

   long HaltUntil() const
   {
      return m_halt_until;
   }
};

#endif // TA_STATE_MQH
