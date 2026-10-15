from arb_functions import *
import time
import pandas as pd
import signal


def do_scenario(i, coin, start, start_time):
    sym, types, pric, bbs = get_trade_type(i, prices, coin)
    amounts = [prices[s]["amount"] for s in sym]

    check = amounts_and_time_check(amounts, usdt, pric, ops, coin, bbs)

    if ops[coin][0] == 0:
        ops[coin][0] = 1
        ops[coin][1] = time.time()

    if not check:
        return

    filled = start
    quantities = []
    print("prices: ", pric)
    print(time.time())
    print(coin)
    # do the three trades
    for i in range(3):
        if i == 2 and not bbs:
            amount = filled * pric[i-1]
        else:
            if types[i] == "BUY":
                amount = filled / pric[i]
            else:
                amount = filled
    
        quantity = truncate(amount, prices[sym[i]]['lot'])
        quantities.append(quantity)
        filled = quantity
        price = '{:0.0{}f}'.format(pric[i], prices[sym[i]]["tick"])
        check_finish_time = time.time()
        if i == 0:
            print(f"check and calculation time: {check_finish_time - start_time}")

        order = place_order(sym[i], types[i], 'LIMIT', quantity, price, client)
        order_placed_time = time.time()
        print(f"order speed: {order_placed_time - check_finish_time}s after request sent, {order_placed_time - start_time}s after start")
        status = order['status']
        speeds_of_status_check = []
        filled = float(order['executedQty'])

        while status != 'FILLED' and status != 'CANCELLED':
            strt = time.time()
            status, filled = get_order_status(symbol=sym[i], order_id=order['orderId'], client=client)
            speeds_of_status_check.append(time.time() - strt)
        filled *= 0.999
        
        if status == 'CANCELLED':
            print(f'user cancelled the trade after {len(speeds_of_status_check)} checks and {sum(speeds_of_status_check) / len(speeds_of_status_check)}s')
            trades['depth1'].append(amounts[0])
            trades['depth2'].append(amounts[1])
            trades['depth3'].append(amounts[2])
            trades['coin'].append(coin)
            trades['bbs'].append(bbs)
            trades['success'].append(False)
            trades['amount1'].append(quantities[0] if len(quantities) > 0 else None)
            trades['amount2'].append(quantities[1] if len(quantities) > 1 else None)
            trades['amount3'].append(quantities[2] if len(quantities) > 2 else None)
            ops[coin][2] = time.time()
            return

        finish_time = time.time() - start_time
        print(f"trade {i+1} filled {finish_time} seconds from the start, {len(speeds_of_status_check)} checks")
        if len(speeds_of_status_check) > 0:
            print(f"{sum(speeds_of_status_check) / len(speeds_of_status_check)}s per check on average")

    trades['coin'].append(coin)
    trades['bbs'].append(bbs)
    trades['amount1'].append(quantities[0])
    trades['amount2'].append(quantities[1])
    trades['amount3'].append(quantities[2])
    trades['depth1'].append(amounts[0])
    trades['depth2'].append(amounts[1])
    trades['depth3'].append(amounts[2])
    success = True if (finish_time) < 10 else False
    trades['success'].append(success)

    print("done")
    ops[coin][2] = time.time()
    #after = get_asset_balance('USDT')
    #print(after)


def process_data():
    while True:
        if len(symbol_queue) == 0:
            continue
        symbol = symbol_queue[0]
        symbol_queue.pop(0)
        start_time = time.time()
        # finds which coin i got the update of
        for coin in coins:
            if symbol in [f"{coin}BTC", f"{coin}USDT", f"{coin}ETH"]:
                updated_coin = coin
        
        # if i got update on one of two starting pairs i check every coin
        if symbol == 'BTCUSDT' or symbol == 'ETHUSDT':
            opportunities = []

            with concurrent.futures.ThreadPoolExecutor() as executor:
                futures = [executor.submit(check_arbitrage_opportunity, coin, prices) for coin in coins]

                for future in concurrent.futures.as_completed(futures):
                    scenario, i, coin = future.result()
                    if scenario > max_profit[0]:
                        max_profit[0] = scenario
                        print(f"maximum: {max_profit[0]}")
                    if scenario > 1:
                        opportunities.append((scenario, i, coin))
                
                if len(opportunities) > 0:
                    biggest_opp = max(opportunities, key=lambda x: x[0])
                    scenario, i, coin = biggest_opp
                    do_scenario(i, coin, usdt[0], start_time)


        # otherwise check only the current coin
        else:
            scenario, i, coin = check_arbitrage_opportunity(updated_coin, prices)
            if scenario < 1:
                if ops[coin][0] == 1:
                    ops[coin][0] = 0
                    print(f"\ntrade took {time.time() - ops[coin][1]}s\n")
            if scenario > max_profit[0]:
                max_profit[0] = scenario
                print(f"maximum: {max_profit[0]}")
            if scenario > 1:
                do_scenario(i, coin, usdt[0], start_time)


# ws functions
def on_message(ws, message):
    data = json.loads(message)
    symbol = data['s']

    if symbol == 'FILUSDT':
        print(data)

    try:
        i = 0
        bid = float(data['b'][0][0])
        amount = float(data['b'][0][1])
        while amount == 0:
            i += 1
            bid = float(data['b'][i][0])
            amount = float(data['b'][i][1])
        prices[symbol]["bid"] = bid
        prices[symbol]["amount"] = amount
    except:
        pass
    try:
        i = 0
        ask = float(data['a'][0][0])
        amount = float(data['a'][0][1])
        while amount == 0:
            i += 1
            ask = float(data['a'][i][0])
            amount = float(data['a'][i][1])
        prices[symbol]["ask"] = ask
        prices[symbol]["amount"] = amount
    except:
        pass
    symbol_queue.append(symbol)


def handle_signal(signum, frame):
    df = pd.DataFrame(trades)
    df.to_csv("trade_data.csv")
    print("killed")
    exit(0)

def on_ping(ws, message):
    print("received ping from server")

def on_pong(ws, message):
    print("received pong from server")

def on_error(ws, error):
    print(error)

def on_close(ws, close_status_code, close_msg):
    print(f"WebSocket closed with code: {close_status_code}, message: {close_msg}")

def on_open(ws):
    for i, symbol in enumerate(symbols):
        time.sleep(0.5)
        if symbol == 'WBTCBTC':
            ws.send(json.dumps({"method": "SUBSCRIBE", "params": [f"{symbol.lower()}@depth"], "id": i}))
        else:
            ws.send(json.dumps({"method": "SUBSCRIBE", "params": [f"{symbol.lower()}@depth@100ms"], "id": i}))
        print(symbol.lower(), "subscribed")

if __name__ == "__main__":
    trades = {"coin":[], "bbs":[], "amount1":[], "amount2":[], "amount3":[], "depth1":[], "depth2":[],
              "depth3":[], "success":[]}
    signal.signal(signal.SIGTERM, handle_signal) # Handle kill command

    client = initiate_client()
    symbols = ['BTCUSDT', 'ETHUSDT']
    coins = ['ltc', 'bnb', 'xrp', 'ada', 'etc', "dot", "inj", 'avax', 'link',
             'near', 'matic', 'atom', 'op', 'fil']
    coins = [coin.upper() for coin in coins]
    for coin in coins:
        symbols.append(f"{coin}BTC")
        symbols.append(f"{coin}USDT")
        symbols.append(f"{coin}ETH")

    usdt = [12]
    prices = {}
    max_profit = [0]
    for symbol in symbols:
        size = float(client.get_symbol_info(symbol)['filters'][0]["tickSize"])
        tick = 0
        while size < 1:
            size *= 10
            tick += 1
        step = float(client.get_symbol_info(symbol)['filters'][1]['stepSize'])
        lot = 0
        while step < 1:
            step *= 10
            lot += 1
        prices[symbol] = {"bid": 0.000000000000000001, "ask": 10000000000000000000,
                          "lot": lot, "tick": tick, "amount": 0}
        print(symbol, "data fetched")
    
    ops = {}
    for coin in coins:
        ops[coin.upper()] = [0, 0, 0]

    symbol_queue = []

    thread = threading.Thread(target=process_data)
    thread.start()
    websocket.enableTrace(False)
    ws = websocket.WebSocketApp("wss://stream.binance.com:9443/ws", on_message=on_message,
                                on_error=on_error,
                                on_close=on_close)
    ws.on_open = on_open
    ws.run_forever(dispatcher=rel, reconnect=5)
    rel.signal(2, rel.abort)
    rel.dispatch()
