-- MCP Bridge: the request kinds besides "snapshot" and "models". Each job's start() returns lists of items to collect
-- (one item per frame slice) and meta() adds request-wide fields; the main script runs them sliced and writes
-- {"meta": ..., "<key>": [...], ...}. Read-only: only the official scripting API's getters are used.

local MOD = (getOwningModId and getOwningModId()) or "mcp_bridge_dev_1"
local collect = ug_require(MOD .. "::/mcp_bridge/mcp_bridge_collect.lua")
local size, at, toArray, try, get, eachPair, num = collect.size, collect.at, collect.toArray, collect.try, collect.get,
	collect.eachPair, collect.num
local cargoName, nameOf, bboxCentre = collect.cargoName, collect.nameOf, collect.bboxCentre

local CT = api.type.ComponentType
local M = { jobs = {} }

local function comp(e, t)
	local ok, c = pcall(function() return api.engine.getComponent(e, t) end)
	if ok then return c end
	return nil
end

local function xy(v)
	local x, y = num(get(v, "x")), num(get(v, "y"))
	if x and y then return { x = math.floor(x + 0.5), y = math.floor(y + 0.5) } end
	return nil
end

local function positionOf(...)
	for _, e in ipairs({ ... }) do
		if e ~= nil then
			local x, y = bboxCentre(e)
			if x then return { x = math.floor(x + 0.5), y = math.floor(y + 0.5) } end
		end
	end
	return nil
end

-- the player's entities with a component; falls back to filtering by PLAYER_OWNED if the filter is refused
local function owned(componentType, player)
	local ok, list = pcall(function()
		return api.engine.getEntitiesWithComponent(componentType, { requireOwnedByPlayer = player })
	end)
	if ok and list ~= nil then return toArray(list) end
	local out = {}
	for _, e in ipairs(toArray(try("getEntitiesWithComponent", function()
		return api.engine.getEntitiesWithComponent(componentType)
	end))) do
		if get(comp(e, CT.PLAYER_OWNED), "player") == player then out[#out + 1] = e end
	end
	return out
end

local function shortRes(name)
	if type(name) ~= "string" then return nil end
	return name:match("([^/]+)%.con$") or name:match("([^/]+)$") or name
end

---------------------------------------------------------------------------------------------------------------------
-- industries: production, inputs, shipping, location

local stock = function() return api.engine.util.stock end

local function industry(id, ctx)
	local I = { id = id, name = nameOf(id) }
	local ind = comp(id, CT.INDUSTRY)
	if ind == nil then return I end
	I.level, I.maxLevel = num(get(ind, "level")), num(get(ind, "maxLevel"))
	I.upgradeProgress = num(get(ind, "upgradeProgress"))
	I.closing = ctx.closing[id] or nil
	local con = get(ind, "construction")
	I.type = shortRes(get(comp(con, CT.CONSTRUCTION), "fileName"))
	I.position = positionOf(con, id)
	local town = try("getConstructionClosestTown", function()
		return api.engine.system.streetConnectorSystem.getConstructionClosestTown(con)
	end)
	if town ~= nil then I.town, I.townId = nameOf(town), town end
	local info = try("getIndustryProductivityInfo", function()
		return api.engine.util.industry.getIndustryProductivityInfo(id)
	end)
	if info then I.producing = get(info, "producing") end
	local sl = get(ind, "stockList")
	if sl == nil then return I end
	I.productionRating = num(try("getProductionRating", function() return stock().getProductionRating(sl) end))
	local io = try("getInputsOutputsFromRules", function() return stock().getInputsOutputsFromRules(sl) end)
	local seen, inputs, outputs = {}, {}, {}
	local ins, outs = io and at(io, 1), io and at(io, 2)
	for k = 1, size(ins) do
		local c = at(ins, k)
		if c ~= nil and not seen["i" .. c] then
			seen["i" .. c] = true
			inputs[#inputs + 1] = {
				cargo = cargoName(c),
				deliveredPerYear = num(try("delivered", function() return stock().getCargoTypeDeliveredPerYear(sl, c) end)),
				consumedPerYear = num(try("consumed", function() return stock().getCargoConsumedPerYear(sl, c) end)),
				maxConsumptionPerYear = num(try("maxConsumption", function() return stock().getCargoMaxConsumptionPerYear(sl, c) end)),
			}
		end
	end
	for k = 1, size(outs) do
		local c = at(outs, k)
		if c ~= nil and not seen["o" .. c] then
			seen["o" .. c] = true
			outputs[#outputs + 1] = {
				cargo = cargoName(c),
				producedPerYear = num(try("produced", function() return stock().getCargoProducedPerYear(sl, c) end)),
				maxProductionPerYear = num(try("maxProduction", function() return stock().getCargoMaxProductionPerYear(sl, c) end)),
				shippedPerYear = num(try("shipped", function() return stock().getCargoTypeShippedPerYear(sl, c) end)),
			}
		end
	end
	if #inputs > 0 then I.inputs = inputs end
	if #outputs > 0 then I.outputs = outputs end
	return I
end

M.jobs.industries = {
	start = function(ctx)
		ctx.closing = {}
		local closing = try("getClosingIndustries", function() return api.engine.util.industry.getClosingIndustries() end)
		for i = 1, size(closing) do
			local pair = at(closing, i)
			local e = pair and at(pair, 1)
			if e ~= nil then ctx.closing[e] = true end
		end
		local list = toArray(try("industries", function() return api.engine.getEntitiesWithComponent(CT.INDUSTRY) end))
		table.sort(list)
		return { { key = "industries", items = list, fn = industry } }
	end,
}

---------------------------------------------------------------------------------------------------------------------
-- stations (per station group) and depots

-- cargo ids a line carries, from its vehicles' capacities (indexed by cargo id + 1)
local function lineCargos(line, ctx)
	local cached = ctx.lineCargo[line]
	if cached then return cached end
	local set, out = {}, {}
	for _, v in ipairs(toArray(try("getLineVehicles", function()
		return api.engine.system.transportVehicleSystem.getLineVehicles(line)
	end))) do
		local caps = get(comp(v, CT.TRANSPORT_VEHICLE), "config", "capacities")
		for i = 1, size(caps) do
			if (num(at(caps, i)) or 0) > 0 and not set[i - 1] then set[i - 1], out[#out + 1] = true, i - 1 end
		end
	end
	table.sort(out)
	ctx.lineCargo[line] = out
	return out
end

local function waitingAt(group, c)
	local q = try("getCargoQualityDataAtStationGroup", function()
		return api.engine.util.cargo.getCargoQualityDataAtStationGroup(group, c)
	end)
	if q == nil then return nil end
	return num(get(q, "countTotal")), get(q, "isVeryBad"), num(get(q, "averageQuality"))
end

local function stationGroup(entry, ctx)
	local g, stations = entry[1], entry[2]
	local S = { id = g, name = nameOf(g), stations = #stations }
	local terminals, used, overflow, pool, termCap, cargoStation, passengerStation = 0, 0, 0, 0, 0, false, false
	for _, st in ipairs(stations) do
		local c = comp(st, CT.STATION)
		local terms = get(c, "terminals")
		terminals = terminals + size(terms)
		for k = 1, size(terms) do
			local t = at(terms, k)
			if get(t, "cargoLoad") or get(t, "cargoUnload") then cargoStation = true end
			if get(t, "passengersLoad") or get(t, "passengersUnload") then passengerStation = true end
		end
		if S.position == nil then S.position = xy(get(c, "catchmentAreas", 1, "position")) end
		local u = try("calculateStationUsage", function() return api.engine.util.station.calculateStationUsage(st) end)
		if u then
			used = used + (num(get(u, "totalUsed")) or 0)
			overflow = overflow + (num(get(u, "overflow")) or 0)
			pool = pool + (num(get(u, "poolCapacity")) or 0)
			termCap = termCap + (num(get(u, "terminalCapacity")) or 0)
		end
	end
	S.terminals = terminals
	S.kind = (cargoStation and passengerStation) and "mixed" or (cargoStation and "cargo")
		or (passengerStation and "passenger") or nil
	S.usage = { used = used, overflow = overflow, poolCapacity = pool, terminalCapacity = termCap }
	local town = try("getTown", function() return api.engine.system.stationSystem.getTown(stations[1]) end)
	if town ~= nil then S.town, S.townId = nameOf(town), town end
	local lines, cargos, seen = {}, {}, {}
	for _, line in ipairs(toArray(try("getLinesForStationGroup", function()
		return api.engine.system.lineSystem.getLinesForStationGroup(g)
	end))) do
		lines[#lines + 1] = { id = line, name = nameOf(line) }
		for _, c in ipairs(lineCargos(line, ctx)) do
			if not seen[c] then seen[c], cargos[#cargos + 1] = true, c end
		end
	end
	S.lines = lines
	local waiting, anyWaiting, veryBad = {}, false, {}
	local stops = toArray(try("getLineStops", function() return api.engine.system.lineSystem.getLineStops(g) end))
	for _, c in ipairs(cargos) do
		local n = 0
		for _, pair in ipairs(stops) do
			local line, stopIndex = at(pair, 1), at(pair, 2)
			n = n + (num(try("getLineStopSimEntitiesCount", function()
				return api.engine.system.simEntityAtTerminalSystem.getLineStopSimEntitiesCount(line, stopIndex, c)
			end)) or 0)
		end
		local q, bad = waitingAt(g, c)
		n = math.max(n, q or 0)
		if n > 0 then
			waiting[cargoName(c)], anyWaiting = n, true
			if bad then veryBad[#veryBad + 1] = cargoName(c) end
		end
	end
	if anyWaiting then S.waiting = waiting end
	if #veryBad > 0 then S.longWaits = veryBad end
	return S
end

local function depot(d, ctx)
	local c = comp(d, CT.VEHICLE_DEPOT)
	local D = { id = d, name = nameOf(d) }
	local modes = {}
	eachPair(get(c, "transportModes"), function(mode, on)
		if on then modes[#modes + 1] = collect.modeName(mode) end
	end)
	local m = table.concat(modes, ",")
	D.carrier = (m:find("TRAM") and "TRAM") or (m:find("TRAIN") and "RAIL") or ((m:find("SHIP")) and "WATER")
		or ((m:find("AIRCRAFT") or m:find("HELICOPTER")) and "AIR") or ((m:find("BUS") or m:find("TRUCK")) and "ROAD")
		or collect.carrierName(get(c, "carrier"))
	D.position = xy(get(c, "catchmentArea", "position")) or positionOf(d)
	D.vehiclesInside = size(try("getDepotVehicles", function()
		return api.engine.system.transportVehicleSystem.getDepotVehicles(d)
	end))
	return D
end

M.jobs.stations = {
	start = function(ctx)
		ctx.lineCargo = {}
		local groups, order = {}, {}
		for _, st in ipairs(owned(CT.STATION, ctx.player)) do
			local g = try("getStationGroup", function() return api.engine.system.stationGroupSystem.getStationGroup(st) end)
			if g ~= nil then
				if groups[g] == nil then groups[g], order[#order + 1] = {}, g end
				table.insert(groups[g], st)
			end
		end
		table.sort(order)
		local items = {}
		for _, g in ipairs(order) do items[#items + 1] = { g, groups[g] } end
		local depots = owned(CT.VEHICLE_DEPOT, ctx.player)
		table.sort(depots)
		return { { key = "stations", items = items, fn = stationGroup }, { key = "depots", items = depots, fn = depot } }
	end,
}

---------------------------------------------------------------------------------------------------------------------
-- station catchment: one owned STATION entity at a time. Bounded summaries, NOT full building/edge dumps.
-- A group can have multiple station entities; deduplicate their catchable entity IDs in the Python MCP tool.
local function catchment(st, ctx)
    local group = try("catchmentStationGroup", function()
        return api.engine.system.stationGroupSystem.getStationGroup(st)
    end)
    local C = { stationId = st, stationGroupId = group, name = group and nameOf(group) or nameOf(st) }
    local town = try("catchmentTown", function() return api.engine.system.stationSystem.getTown(st) end)
    if town then C.town, C.townId = nameOf(town), town end
    C.position = positionOf(st)
    local sys = api.engine.system.catchmentAreaSystem
    -- Limit exported sample IDs. Counts describe the whole returned list, samples do not.
    for _, entry in ipairs({ {"passenger", false}, {"cargo", true} }) do
        local key, cargo = entry[1], entry[2]
        local ok, hits = pcall(function() return sys.getStationCatchables(st, cargo) end)
        if ok and hits ~= nil then
            local sample, full, seen, count = {}, {}, {}, 0
            for i = 1, size(hits) do
                local e = at(hits, i)
                if e ~= nil and not seen[e] then
                    seen[e] = true; count = count + 1
                    if #sample < 64 then sample[#sample + 1] = e end
                    if count <= 1024 then full[#full + 1] = e end
                end
            end
            C[key] = { count = count, sampleEntityIds = sample, sampleLimit = 64, truncated = count > #sample,
                fullEntityIds = count <= 1024 and full or nil, fullEntityLimit = 1024, exactIdsAvailable = count <= 1024 }
        else
            C[key] = { error = tostring(hits), supported = false }
        end
    end
    return C
end

M.jobs.catchment = {
    start = function(ctx)
        local stations = owned(CT.STATION, ctx.player)
        table.sort(stations)
        return { { key = "stationCatchments", items = stations, fn = catchment } }
    end,
}

---------------------------------------------------------------------------------------------------------------------
-- v7.6 cargo access diagnostics. This is a read-only *membership* check, not proof of loading.
-- Each item processes one station entity so work remains frame-sliced by the bridge scheduler.

local function cargoAccess(st, ctx)
    local group = try("cargoAccessGroup", function()
        return api.engine.system.stationGroupSystem.getStationGroup(st)
    end)
    local c = comp(st, CT.STATION)
    local A = { stationId = st, stationGroupId = group, stationName = group and nameOf(group) or nameOf(st) }
    local town = try("cargoAccessTown", function() return api.engine.system.stationSystem.getTown(st) end)
    if town ~= nil then A.town, A.townId = nameOf(town), town end
    A.position = positionOf(st)
    local terms = get(c, "terminals")
    local caps = { cargoLoad = false, cargoUnload = false, passengerLoad = false, passengerUnload = false,
                   terminalCount = size(terms) }
    for i = 1, size(terms) do
        local term = at(terms, i)
        if get(term, "cargoLoad") then caps.cargoLoad = true end
        if get(term, "cargoUnload") then caps.cargoUnload = true end
        if get(term, "passengersLoad") then caps.passengerLoad = true end
        if get(term, "passengersUnload") then caps.passengerUnload = true end
    end
    A.terminalCapabilities = caps
    local ok, hits = pcall(function()
        return api.engine.system.catchmentAreaSystem.getStationCatchables(st, true)
    end)
    if not ok or hits == nil then
        A.catchables = { supported = false, error = tostring(hits) }
    else
        local count, matches, seen = 0, {}, {}
        for i = 1, size(hits) do
            local id = at(hits, i)
            if id ~= nil and not seen[id] then
                seen[id] = true
                count = count + 1
                if ctx.industryIds[id] then matches[#matches + 1] = id end
            end
        end
        table.sort(matches)
        A.catchables = { supported = true, totalUniqueCargoEntityIds = count,
            matchingIndustryIds = matches, exactMembershipTest = true,
            semantics = "Station catchables contain these INDUSTRY entity IDs; loading and unloading are NOT proven" }
    end
    return A
end

M.jobs.cargo_access = {
    start = function(ctx)
        ctx.industryIds = {}
        for _, id in ipairs(toArray(try("cargoAccessIndustries", function()
            return api.engine.getEntitiesWithComponent(CT.INDUSTRY)
        end))) do ctx.industryIds[id] = true end
        local stations = owned(CT.STATION, ctx.player)
        table.sort(stations)
        return { { key = "cargoAccessStations", items = stations, fn = cargoAccess } }
    end,
}

---------------------------------------------------------------------------------------------------------------------
-- v7.8: diagnostics for town-building membership in official cargo station catchables.
-- Read-only; no inference about goods acceptance, building demand, or viable delivery.
-- Index town buildings once per request, then process one station entity per frame slice.
local function townDelivery(st, ctx)
    local group = try("deliveryGroup", function() return api.engine.system.stationGroupSystem.getStationGroup(st) end)
    local station = comp(st, CT.STATION)
    local terms = get(station, "terminals")
    local load, unload = false, false
    for i = 1, size(terms) do
        local term = at(terms, i)
        if get(term,"cargoLoad") then load = true end
        if get(term,"cargoUnload") then unload = true end
    end
    local D = { stationId=st, stationGroupId=group, name=group and nameOf(group) or nameOf(st),
        cargoLoadTerminal=load, cargoUnloadTerminal=unload }
    local ok, hits = pcall(function() return api.engine.system.catchmentAreaSystem.getStationCatchables(st, true) end)
    if not ok or hits == nil then
        D.catchablesSupported = false; D.error = tostring(hits); return D
    end
    D.catchablesSupported = true
    local counts, samples, seen, total = {}, {}, {}, 0
    for i=1,size(hits) do
        local eid = at(hits,i)
        if eid ~= nil and not seen[eid] then
            seen[eid] = true; total = total + 1
            local t = ctx.deliveryBuildingTown[eid]
            if t ~= nil then
                counts[t] = (counts[t] or 0) + 1
                local k = tostring(t)
                local list = samples[k]
                if list == nil then list = {}; samples[k] = list end
                if #list < 12 then
                    list[#list + 1] = { buildingId = eid, position = positionOf(eid) }
                end
            end
        end
    end
    D.uniqueCargoCatchables = total
    D.matchedTowns = {}
    for t,n in pairs(counts) do
        D.matchedTowns[#D.matchedTowns+1] = { townId=t, town=nameOf(t), matchingTownBuildingIds=n,
           sampleBuildings=samples[tostring(t)] or {}, sampleLimit=12 }
    end
    table.sort(D.matchedTowns, function(a,b) return a.townId < b.townId end)
    return D
end

M.jobs.town_delivery = {
    start = function(ctx)
        ctx.deliveryBuildingTown = {}
        ctx.deliveryTownBuildingTotals = {}
        local map = try("getTown2BuildingMapDelivery", function()
            return api.engine.system.townBuildingSystem.getTown2BuildingMap()
        end)
        eachPair(map, function(town, buildings)
            local n = 0
            for i=1,size(buildings) do
                local b = at(buildings, i)
                if b ~= nil then ctx.deliveryBuildingTown[b] = town; n = n + 1 end
            end
            ctx.deliveryTownBuildingTotals[town] = n
        end)
        local stations = owned(CT.STATION, ctx.player)
        table.sort(stations)
        return {{ key="townDeliveryStations", items=stations, fn=townDelivery }}
    end,
}

---------------------------------------------------------------------------------------------------------------------
-- finances: account, company value, finance table (per carrier and category, as the game's finance window)

local function plain(v, depth)
	depth = depth or 0
	if depth > 4 then return nil end
	if type(v) == "number" or type(v) == "string" or type(v) == "boolean" then return v end
	local out, n = {}, size(v)
	if n > 0 then
		for i = 1, n do out[i] = plain(at(v, i), depth + 1) end
		return out
	end
	local any = false
	eachPair(v, function(k, x)
		out[tostring(k)] = plain(x, depth + 1)
		any = true
	end)
	return any and out or nil
end

local COMPANY_FIELDS = { "totalScore", "railVehicles", "trams", "roadVehicles", "aircrafts", "ships", "trackTotalLength",
	"trackElectricLength", "bridgeTotalLength", "tunnelTotalLength", "roadTotalLength", "suppliedTowns",
	"connectedIndustries", "numberOfLines", "totalStations", "railStations", "tramStations", "roadStations",
	"aircraftStations", "shipStations", "topSpeed", "topLength", "balance", "totalAssets", "debt" }

local function finances(_, ctx)
	local F = {}
	local player = ctx.player
	F.balance = num(try("getPlayersBalance", function() return api.engine.util.finance.getPlayersBalance(player) end))
	local acc = comp(player, CT.ACCOUNT)
	if acc then F.loan = num(get(acc, "loan")) end
	F.earningsThisYear = num(try("calculateEarnings", function() return api.engine.util.finance.calculateEarnings(player) end))
	local cv = try("getCompaniesValue", function() return api.engine.util.headquarters.getCompaniesValue() end)
	if cv then
		local company = {}
		for _, k in ipairs(COMPANY_FIELDS) do company[k] = num(get(cv, k)) end
		F.company = company
	end
	local td = try("getTransportedData", function() return api.engine.util.headquarters.getTransportedData() end)
	if td then
		F.transported = { passengers = num(get(td, "passengersTransported")), cargo = num(get(td, "cargoTransported")) }
	end
	-- the finance window's table: one column per year (best effort; the layout is undocumented)
	local fd = try("computeFinanceTable", function()
		local cfg = api.type.ChartConfig.new()
		cfg.interval = ctx.yearTicks and math.floor(ctx.yearTicks) or cfg.interval
		cfg.count = 5
		return api.engine.util.finance.computeFinanceTable(player, cfg)
	end)
	if fd then
		local t = {}
		for _, k in ipairs({ "header", "total", "balance", "loan", "interest", "loanBorrowing", "loanRepayment" }) do
			t[k] = plain(get(fd, k))
		end
		local carriers = {}
		pcall(function()
			fd:foreach_carrier(function(carrier, map)
				carriers[tostring(carrier)] = plain(map)
			end)
		end)
		t.transportByCarrier = carriers
		t.investment = plain(get(fd, "investment"))
		F.financeTable = t
	end
	return F
end

M.jobs.finances = {
	start = function(ctx)
		return { { key = "finances", items = { 1 }, fn = finances } }
	end,
}

---------------------------------------------------------------------------------------------------------------------
-- vehicles live: position, state and recent departures of every line vehicle (for bunching and stuck vehicles)

local function liveVehicle(item, ctx)
	local v, line = item[1], item[2]
	local tv = comp(v, CT.TRANSPORT_VEHICLE)
	local V = { id = v, line = line, lineName = ctx.lineNames[line], name = nameOf(v) }
	V.position = xy(try("getPosition", function() return api.engine.util.vehicle.getPosition(v) end))
	V.speed = num(try("getSpeed", function() return api.engine.util.vehicle.getSpeed(v) end))
	if tv then
		V.state = collect.stateName(get(tv, "state"))
		V.stopIndex = num(get(tv, "stopIndex"))
		V.doorsOpen = get(tv, "doorsOpen")
		V.noPath = get(tv, "noPath")
		V.userStopped = get(tv, "userStopped")
		V.timeUntilDeparture = num(get(tv, "timeUntilDeparture"))
		V.lastDeparture = num(get(tv, "lastLineStopDeparture"))
		local deps = get(tv, "lineStopDepartures")
		local list = {}
		for i = 1, size(deps) do list[i] = num(at(deps, i)) or 0 end
		V.stopDepartures = list
		V.daysAtTerminal = num(get(tv, "daysAtTerminal"))
	end
	V.load = num(try("getVehicleSimEntitiesCount", function()
		return api.engine.system.simEntityAtVehicleSystem.getVehicleSimEntitiesCount(v)
	end))
	return V
end

M.jobs.vehicles = {
	start = function(ctx)
		local items = {}
		ctx.lineNames = {}
		for _, line in ipairs(toArray(try("getLinesForPlayer", function()
			return api.engine.system.lineSystem.getLinesForPlayer(ctx.player)
		end))) do
			ctx.lineNames[line] = nameOf(line)
			for _, v in ipairs(toArray(try("getLineVehicles", function()
				return api.engine.system.transportVehicleSystem.getLineVehicles(line)
			end))) do
				items[#items + 1] = { v, line }
			end
		end
		return { { key = "vehicles", items = items, fn = liveVehicle } }
	end,
}

return M
