//+------------------------------------------------------------------+
//| Socket.mqh - newline-delimited TCP client used by the EA         |
//|                                                                  |
//| MQL5 only supports outbound sockets, so the EA is the client and |
//| Python is the loopback listener.  Sockets are unavailable in the |
//| Strategy Tester; the tester path never constructs this class.    |
//+------------------------------------------------------------------+
#ifndef TA_SOCKET_MQH
#define TA_SOCKET_MQH

#include <TradingAgents/Types.mqh>
#include <TradingAgents/Protocol.mqh>

class TASocket
{
private:
   int      m_socket;
   uchar    m_rx[];
   uint     m_last_connect_ms;

   void CompactLine(const int bytes)
   {
      if(bytes <= 0)
         return;
      int size = ArraySize(m_rx);
      if(bytes >= size)
      {
         ArrayResize(m_rx, 0);
         return;
      }
      uchar rest[];
      ArrayResize(rest, size - bytes);
      ArrayCopy(rest, m_rx, 0, bytes, size - bytes);
      ArrayResize(m_rx, size - bytes);
      ArrayCopy(m_rx, rest, 0, 0, size - bytes);
   }

public:
   TASocket()
   {
      m_socket = INVALID_HANDLE;
      m_last_connect_ms = 0;
      ArrayResize(m_rx, 0);
   }

   ~TASocket()
   {
      Close();
   }

   bool IsOpen() const
   {
      return (m_socket != INVALID_HANDLE);
   }

   int Handle() const
   {
      return m_socket;
   }

   bool Connect(const string host, const int port, const int timeout_ms,
                string &error)
   {
      error = "";
      Close();
      m_socket = SocketCreate();
      if(m_socket == INVALID_HANDLE)
      {
         error = StringFormat("SocketCreate failed: %d", GetLastError());
         return false;
      }
      if(!SocketConnect(m_socket, host, (uint)port, (uint)timeout_ms))
      {
         error = StringFormat("SocketConnect(%s:%d) failed: %d",
                              host, port, GetLastError());
         SocketClose(m_socket);
         m_socket = INVALID_HANDLE;
         return false;
      }
      SocketTimeouts(m_socket, 50, 500);
      ArrayResize(m_rx, 0);
      m_last_connect_ms = GetTickCount();
      return true;
   }

   void Close()
   {
      if(m_socket != INVALID_HANDLE)
      {
         SocketClose(m_socket);
         m_socket = INVALID_HANDLE;
      }
      ArrayResize(m_rx, 0);
   }

   bool IsConnected()
   {
      if(m_socket == INVALID_HANDLE)
         return false;
      return SocketIsConnected(m_socket);
   }

   bool SendLine(const string line)
   {
      if(m_socket == INVALID_HANDLE)
         return false;
      string data = line + "\n";
      uchar buffer[];
      int length = StringToCharArray(data, buffer, 0, WHOLE_ARRAY, CP_UTF8) - 1;
      if(length <= 0)
         return false;
      if(!SocketSend(m_socket, buffer, (uint)length))
      {
         PrintFormat("EA socket send failed: %d", GetLastError());
         return false;
      }
      return true;
   }

   // Returns one complete line when available.  Call in a loop until false.
   bool Poll(string &line)
   {
      line = "";
      if(m_socket == INVALID_HANDLE)
         return false;

      int guard = 0;
      while(guard++ < 16)
      {
         uint available = SocketIsReadable(m_socket);
         if(available == 0)
            break;
         uchar buffer[];
         int want = (available > 8192) ? 8192 : (int)available;
         ArrayResize(buffer, want);
         int received = SocketRead(m_socket, buffer, (uint)want, 10);
         if(received <= 0)
            break;
         int old = ArraySize(m_rx);
         if(old + received > TA_MAX_FRAME_BYTES * 4)
         {
            Print("EA socket receive buffer exceeded; dropping connection");
            Close();
            return false;
         }
         ArrayResize(m_rx, old + received);
         ArrayCopy(m_rx, buffer, old, 0, received);
      }

      int size = ArraySize(m_rx);
      if(size == 0)
         return false;
      int newline = -1;
      for(int i = 0; i < size; i++)
      {
         if(m_rx[i] == 10)
         {
            newline = i;
            break;
         }
      }
      if(newline < 0)
      {
         if(size > TA_MAX_FRAME_BYTES)
         {
            Print("EA socket frame exceeded 1 MiB without newline; dropping connection");
            Close();
         }
         return false;
      }

      uchar raw[];
      ArrayResize(raw, newline);
      if(newline > 0)
         ArrayCopy(raw, m_rx, 0, 0, newline);
      line = CharArrayToString(raw, 0, newline, CP_UTF8);
      CompactLine(newline + 1);
      int end = StringLen(line);
      while(end > 0)
      {
         ushort c = StringGetCharacter(line, end - 1);
         if(c != '\r' && c != '\n' && c != ' ' && c != '\t')
            break;
         end--;
      }
      line = StringSubstr(line, 0, end);
      return (StringLen(line) > 0);
   }
};

#endif // TA_SOCKET_MQH
