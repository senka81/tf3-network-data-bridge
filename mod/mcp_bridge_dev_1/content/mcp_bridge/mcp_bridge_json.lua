-- Minimal JSON encoder for the bridge snapshot. Tables with keys 1..n (and nothing else) become arrays, everything
-- else becomes an object with string keys. NaN and infinities become null; userdata becomes its tostring().

local M = {}

local escapes = {
	['"'] = '\\"', ['\\'] = '\\\\', ['\b'] = '\\b', ['\f'] = '\\f', ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t',
}

local function escapeString(s)
	return '"' .. s:gsub('[%c"\\]', function(c)
		return escapes[c] or string.format("\\u%04x", c:byte())
	end) .. '"'
end

local function encodeNumber(n)
	if n ~= n or n == math.huge or n == -math.huge then return "null" end
	if n == math.floor(n) and math.abs(n) < 2 ^ 53 then return string.format("%d", n) end
	return string.format("%.10g", n)
end

local function isArray(t)
	local n = 0
	for k in pairs(t) do
		if type(k) ~= "number" or k < 1 or k ~= math.floor(k) then return false end
		n = n + 1
	end
	for i = 1, n do
		if t[i] == nil then return false end
	end
	return true, n
end

local encode

local function encodeTable(t, out, depth)
	if depth > 32 then out[#out + 1] = "null"; return end
	local array, n = isArray(t)
	if array and n > 0 then
		out[#out + 1] = "["
		for i = 1, n do
			if i > 1 then out[#out + 1] = "," end
			encode(t[i], out, depth + 1)
		end
		out[#out + 1] = "]"
		return
	end
	if array then out[#out + 1] = "{}"; return end -- empty table
	out[#out + 1] = "{"
	local first = true
	for k, v in pairs(t) do
		if v ~= nil and type(v) ~= "function" then
			if not first then out[#out + 1] = "," end
			first = false
			out[#out + 1] = escapeString(tostring(k))
			out[#out + 1] = ":"
			encode(v, out, depth + 1)
		end
	end
	out[#out + 1] = "}"
end

encode = function(v, out, depth)
	local tv = type(v)
	if v == nil then out[#out + 1] = "null"
	elseif tv == "boolean" then out[#out + 1] = v and "true" or "false"
	elseif tv == "number" then out[#out + 1] = encodeNumber(v)
	elseif tv == "string" then out[#out + 1] = escapeString(v)
	elseif tv == "table" then encodeTable(v, out, depth)
	else out[#out + 1] = escapeString(tostring(v)) end
end

function M.encode(v)
	local out = {}
	encode(v, out, 0)
	return table.concat(out)
end

-- JSON array of already encoded items
function M.array(items)
	return "[" .. table.concat(items, ",") .. "]"
end

return M
